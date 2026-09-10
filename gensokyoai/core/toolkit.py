"""工具执行器 —— 全项目**唯一**的工具执行点。

此前执行逻辑在 `models/base.py._execute_tools`（Provider 层工具循环）和
`core/brain/engine.py._execute_tool_calls`（接力思考循环）里**各写了一遍**：
查表 → 解析参数 JSON → invoke/ainvoke → catch → 转字符串。两份实现必然漂移。

本模块把这些统一到 `ToolExecutor`，并补上原先缺的四件事：

- **结果截断**：工具返回大字符串会撑爆 8K 上下文，超长一律截断并标注
- **超时**：工具不再能永久挂住调用链
- **同步工具下线程**：`asyncio.to_thread` 执行同步工具，避免阻塞事件循环
- **结构化错误**：`ToolResult(ok=False, error=...)` 取代裸字符串
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import msgspec

from ..schemas.model_schema import ToolCall, ToolSpec
from ..utils.logger import LoggerManager

DEFAULT_TIMEOUT = 10.0
""" 单次工具执行默认超时（秒）"""

DEFAULT_MAX_RESULT_CHARS = 2000
""" 工具结果默认最大字符数，超出截断（保护 8K 上下文）"""

_TRUNCATED_MARK = "…（结果过长已截断）"


@dataclass(slots=True)
class ToolResult:
    """一次工具执行的结果。"""

    name: str
    """ 工具名 """
    ok: bool = True
    """ 是否成功 """
    content: str = ""
    """ 成功时的结果文本（已截断）"""
    error: str = ""
    """ 失败原因（人类可读）"""
    elapsed_s: float = 0.0
    """ 耗时（秒）"""

    def to_model_text(self) -> str:
        """回填给模型的文本表示（成功给内容，失败给错误）。

        Returns:
            str: 可直接塞进提示词/工具消息的文本
        """
        return self.content if self.ok else f"工具 {self.name} 执行失败：{self.error}"


@dataclass(slots=True)
class ToolExecutor:
    """统一的工具执行器：查表 + 解析参数 + 执行 + 截断 + 错误兜底。

    两个调用方（Provider 工具循环 / 接力思考循环）都改用它，
    用 `ToolResult.to_model_text()` 回填给模型。
    """

    tools: list[ToolSpec] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    _index: dict[str, ToolSpec] = field(default_factory=dict, init=False)
    _logger: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._logger = LoggerManager.get_logger("TOOLKIT")
        self.reindex()

    def reindex(self) -> None:
        """重建名字索引（工具集合变化后调用）。"""
        self._index = {tool.tool_name: tool for tool in self.tools}

    @property
    def names(self) -> list[str]:
        """已注册工具名列表。"""
        return list(self._index)

    def get(self, name: str) -> ToolSpec | None:
        """按名取工具；不存在返回 None。"""
        return self._index.get(name)

    async def execute(self, name: str, arguments: str | dict | None = None) -> ToolResult:
        """执行一次工具调用（**不抛异常**，失败也返回结构化结果）。

        Args:
            name: 工具名
            arguments: 参数（JSON 字符串或已解析的 dict）；None/空视为无参

        Returns:
            ToolResult: 结构化结果
        """
        started = time.monotonic()
        tool = self._index.get(name)
        if tool is None:
            self._logger.warning(f"未知工具调用: {name}")
            return ToolResult(name=name, ok=False, error=f"未知工具 {name}")

        args = self._parse_args(arguments)
        if args is None:
            self._logger.warning(f"工具参数 JSON 解析失败: {name}({arguments!r})")
            return ToolResult(name=name, ok=False, error="参数不是合法的 JSON 对象")

        try:
            output = await asyncio.wait_for(self._invoke(tool, args), timeout=self.timeout)
        except TimeoutError:
            elapsed = time.monotonic() - started
            self._logger.warning(f"工具超时: {name}({args}) 超过 {self.timeout:g}s")
            return ToolResult(
                name=name, ok=False, error=f"执行超时（>{self.timeout:g}s）", elapsed_s=elapsed
            )
        except Exception as err:
            elapsed = time.monotonic() - started
            self._logger.exception(f"工具执行异常: {name}({args})")
            return ToolResult(
                name=name, ok=False, error=f"{type(err).__name__}: {err}", elapsed_s=elapsed
            )

        content = self._truncate(str(output))
        elapsed = time.monotonic() - started
        self._logger.info(f"工具调用: {name}({args}) -> {content[:80]!r} 耗时={elapsed:.2f}s")
        return ToolResult(name=name, ok=True, content=content, elapsed_s=elapsed)

    async def execute_many(self, calls: list[ToolCall]) -> list[ToolResult]:
        """顺序执行一批工具调用。

        Args:
            calls: 模型请求的工具调用列表

        Returns:
            list[ToolResult]: 与入参一一对应的结果
        """
        return [await self.execute(call.name, call.arguments) for call in calls]

    async def _invoke(self, tool: ToolSpec, args: dict) -> Any:
        """调用工具本体：异步直接 await，同步下线程跑（不阻塞事件循环）。"""
        if tool.is_async:
            return await tool.tool_func(**args)
        return await asyncio.to_thread(tool.tool_func, **args)

    @staticmethod
    def _parse_args(arguments: str | dict | None) -> dict | None:
        """把参数解析成 dict；非法 JSON 返回 None。"""
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = msgspec.json.decode(arguments, type=dict)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _truncate(self, text: str) -> str:
        """超长结果截断并标注。"""
        if self.max_result_chars <= 0 or len(text) <= self.max_result_chars:
            return text
        keep = max(0, self.max_result_chars - len(_TRUNCATED_MARK))
        return text[:keep] + _TRUNCATED_MARK


def build_executor(
    tools: list[ToolSpec] | None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> ToolExecutor:
    """由工具列表构建执行器（调用方的便捷入口）。

    Args:
        tools: 工具声明列表；None 视为空
        timeout: 单次执行超时（秒）
        max_result_chars: 结果最大字符数

    Returns:
        ToolExecutor: 执行器实例
    """
    return ToolExecutor(tools=list(tools or []), timeout=timeout, max_result_chars=max_result_chars)


__all__ = [
    "DEFAULT_MAX_RESULT_CHARS",
    "DEFAULT_TIMEOUT",
    "ToolExecutor",
    "ToolResult",
    "build_executor",
]

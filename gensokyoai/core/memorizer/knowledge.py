"""知识缓存 —— 联网工具（web_search / fetch_url）的二级缓存。

神经科学梗：人脑的记忆分情景记忆（经历过的对话）与语义记忆（学来的知识）。
memorizer 里的对话蒸馏是前者；这里缓存的搜索结果就是后者——同一套存储、
同一套向量检索，只是 `MemoryType.KNOWLEDGE` 区分。

- **L1 会话内精确缓存**：归一化查询 -> (结果, 时间戳)，TTL 内命中直接返回，
  零网络零 token（同一话题短时间内反复触发搜索的场景）
- **L2 长期记忆**：成功结果存成 KNOWLEDGE 条目（importance 过归档线，自动进
  长期记忆并向量化）——以后聊到相关话题，`recent(search_term=...)` 关联检索
  自然把它捞进上下文，模型「想起来」就不再重复搜索

时效性保护：查询命中时效词（今天/最新/价格……）的结果只进 L1 不进 L2
——新闻会过期，设定不会。
"""

import asyncio
import functools
import inspect
import re
import time
from collections.abc import Callable

from ...schemas.memory_schema import MemoryItem, MemoryType
from ...schemas.model_schema import ToolSpec
from ...utils.logger import LoggerManager
from .manager import MemoryManager

_FRESHNESS_WORDS = (
    "今天",
    "今日",
    "现在",
    "当前",
    "最新",
    "新闻",
    "价格",
    "版本",
    "发布",
    "更新",
    "today",
    "latest",
    "news",
)
""" 时效词：查询命中这些词的结果不进 L2 长期记忆（会过期） """

_KNOWLEDGE_MAX_CHARS = 2000
""" 单条知识入库上限（与工具结果截断对齐） """

_KNOWLEDGE_IMPORTANCE = 0.6
""" 知识条目重要性：刚好过归档线（0.6），直接进长期记忆并向量化 """

_FAILURE_PREFIXES = ("搜索失败", "抓取失败", "联网搜索不可用", "没有找到", "这个地址不允许")
""" 工具失败/空结果的返回前缀（工具是我们自己的，前缀稳定）：不缓存不归档 """

_NORMALIZE_WS = re.compile(r"\s+")


class KnowledgeCache:
    """联网工具的二级缓存。每个世界一份（L1 会话内有效；L2 走该世界的记忆系统）。"""

    def __init__(self, memory: MemoryManager, ttl_s: float = 1800.0) -> None:
        """初始化。

        Args:
            memory: 本世界的记忆管理器（L2 归档出口）
            ttl_s: L1 精确缓存的有效秒数（默认 30 分钟）
        """
        self._logger = LoggerManager.get_logger("KNOWLEDGE")
        self._memory = memory
        self._ttl_s = ttl_s
        self._l1: dict[str, tuple[float, str]] = {}

    def wrap_all(self, tools: list[ToolSpec]) -> list[ToolSpec]:
        """把 web_search / fetch_url 换成带缓存的包装版（其余工具原样返回）。

        包装保持原签名与文档（functools.wraps），对模型与工具链路完全透明。
        """
        wrapped: list[ToolSpec] = []
        for spec in tools:
            if spec.tool_name == "web_search":
                wrapped.append(self._wrap_web_search(spec))
                self._logger.debug("web_search 已套知识缓存")
            elif spec.tool_name == "fetch_url":
                wrapped.append(self._wrap_fetch_url(spec))
                self._logger.debug("fetch_url 已套知识缓存")
            else:
                wrapped.append(spec)
        return wrapped

    def _wrap_web_search(self, spec: ToolSpec) -> ToolSpec:
        """web_search 的缓存包装（独立方法：避免闭包捕获循环变量）。"""
        original = spec.tool_func

        @functools.wraps(original)
        async def cached_web_search(query: str, max_results: int = 5) -> str:
            return await self._cached(f"web:{query}", original, query, max_results)

        return ToolSpec(tool_func=cached_web_search, name=spec.name)

    def _wrap_fetch_url(self, spec: ToolSpec) -> ToolSpec:
        """fetch_url 的缓存包装（独立方法：避免闭包捕获循环变量）。"""
        original = spec.tool_func

        @functools.wraps(original)
        async def cached_fetch_url(url: str) -> str:
            return await self._cached(f"fetch:{url}", original, url)

        return ToolSpec(tool_func=cached_fetch_url, name=spec.name)

    async def _cached(self, key: str, original: Callable, *args) -> str:
        """缓存主流程：L1 命中直接返回；未命中执行原工具，成功则写 L1 + 归档 L2。"""
        now = time.monotonic()
        hit = self._l1.get(key)
        if hit is not None and now - hit[0] < self._ttl_s:
            self._logger.debug(f"L1 命中: {key[:60]}")
            return hit[1] + "\n（知识缓存命中）"

        if inspect.iscoroutinefunction(original):
            result = await original(*args)
        else:
            result = await asyncio.to_thread(original, *args)

        if isinstance(result, str) and not result.startswith(_FAILURE_PREFIXES):
            self._l1[key] = (now, result)
            await self._archive(key, result)
        return result

    async def _archive(self, key: str, result: str) -> None:
        """L2 归档：时效词查询跳过（新闻会过期，设定不会）；归档失败不拖垮工具调用。"""
        query = key.split(":", 1)[1]
        if any(w in query.lower() for w in _FRESHNESS_WORDS):
            self._logger.debug(f"时效词命中，不进长期记忆: {query[:40]}")
            return
        item = MemoryItem(
            topic=f"知识：{query[:30]}",
            content=result[:_KNOWLEDGE_MAX_CHARS],
            memory_type=MemoryType.KNOWLEDGE,
            importance=_KNOWLEDGE_IMPORTANCE,
        )
        try:
            await self._memory.store(item)
            self._logger.info(f"知识归档: {query[:40]} -> 长期记忆")
        except Exception:
            self._logger.exception("知识归档失败（不影响工具返回）")


__all__ = ["KnowledgeCache"]

"""前缀缓存管理 —— 按挥发度排版 prompt，观测并守住前缀缓存的命中率。

职责边界：缓存本身是厂商（DeepSeek context cache / OpenAI 前缀缓存）或服务端
（llama-server 的 slot 前缀复用）做的，本类唯一能控制的是**请求的形状**：

- **版面纪律**：段按挥发度升序排（static 在前、volatile 垫底）。前缀缓存按
  最长公共前缀匹配，第一个不同的字节之后全部重算——所以易变内容必须永远
  落在尾部，这是排版契约，不靠调用方自觉；
- **漂移检测**：声明为 static 的段按名记指纹（hash），同名内容变了 = 漂移
  warning——防有人顺手往系统区拼时间戳/计数器，缓存悄悄全灭还无人知晓；
- **撤回语义**：`retract(n)` 从尾部静默弹消息。角色扮演的消息回退（撤回
  最后一问一答再问一遍）只脏尾部——截尾点之前的每个字节都没变，缓存继续
  命中；绝不往历史里插「用户撤回了一条消息」的注解（注解改字节 = 前缀报废）；
- **命中率遥测**：`record_usage()` 记 cached/prompt token，`stats()` 给出
  命中率与漂移计数（接 health / 将来的 /doctor 的素材）。

排版分工：`prompts/manager.py` 管「渲染什么」（模板内容），本模块管
「怎么排才不浪费」（版面与记账）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ..schemas.model_schema import Message
from ..utils.logger import LoggerManager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ..schemas.model_schema import Usage

type Role = Literal["system", "user", "assistant", "tool"]
""" 消息角色（与 schemas.model_schema.Message.role 同口径）"""


def _hash(text: str) -> str:
    """段内容指纹（8 位短 hash，日志可读）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


@dataclass
class _StaticSegment:
    """一个声明为稳定的段（系统区）。"""

    name: str
    role: Role
    content: str
    fingerprint: str


@dataclass(frozen=True)
class CacheStats:
    """命中率遥测快照（某 owner 的累计口径）。"""

    calls: int = 0
    """ 记账的调用次数 """
    prompt_tokens: int = 0
    """ 累计输入 token """
    cached_tokens: int = 0
    """ 累计命中前缀缓存的输入 token """
    drifts: dict[str, int] = field(default_factory=dict)
    """ 各稳定段的漂移次数（段名 -> 次数）"""

    @property
    def hit_rate(self) -> float:
        """前缀缓存命中率（cached / prompt；无调用为 0）。"""
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


class PromptCache:
    """前缀缓存管理器：声明挥发度 -> 排版 -> 渲染 -> 记账。

    生命周期（跨调用持有，漂移检测才有意义）：

    ```python
    pc = PromptCache(owner="brain.think")
    pc.static_msg("system", persona_block, name="persona")   # 跨调用稳定
    # 每次调用：
    pc.reset_tail()                                          # 清上一轮尾部
    pc.volatile_msg("user", compiled_context)                # 每轮编译，垫底
    messages = pc.render()
    # 追加增量（接力思考轮次 / 工具结果 / 新历史）：
    pc.extend(extra_messages)
    # 撤回最后一问一答（消息回退）：
    pc.retract(2)
    ```
    """

    def __init__(self, owner: str, *, log_every: int = 0) -> None:
        """初始化。

        Args:
            owner: 模块标识（日志与统计的归属名，如 brain.think / responder）
            log_every: 每记账 N 次调用落一行命中率汇总日志；0 = 不主动落
        """
        self._logger = LoggerManager.get_logger("PROMPT-CACHE")
        self._owner = owner
        self._log_every = log_every
        self._static: dict[str, _StaticSegment] = {}
        """ 稳定段（名 -> 段；dict 保登记序 = 渲染序）"""
        self._tail: list[Message] = []
        """ 尾部消息（volatile + extend 追加的增量，按顺序）"""
        self._calls = 0
        self._prompt_tokens = 0
        self._cached_tokens = 0
        self._drifts: dict[str, int] = {}

    # ---------------------------------------------------------------- 稳定段

    def static_msg(self, role: Role, content: str, *, name: str) -> None:
        """登记一个稳定段（必须在任何尾部内容之前）。

        同名同内容 = no-op（跨调用重复登记是正常用法）；同名不同内容 =
        **漂移**：warning 落日志并更新指纹（system 前缀从这一段起全部重算）。

        Args:
            role: 消息角色（通常 "system"）
            content: 段内容
            name: 段名（漂移日志与统计的标识）

        Raises:
            ValueError: 已有尾部内容后再登记稳定段（排版错误早暴露）
        """
        if self._tail:
            raise ValueError(
                f"[{self._owner}] 稳定段 {name!r} 登记在尾部内容之后："
                "稳定段必须先于所有 volatile/extend 内容（版面纪律）"
            )
        fingerprint = _hash(content)
        existing = self._static.get(name)
        if existing is not None:
            if existing.fingerprint == fingerprint:
                return  # 同名同内容：no-op
            self._drifts[name] = self._drifts.get(name, 0) + 1
            self._logger.warning(
                f"[{self._owner}] 稳定段漂移: {name} "
                f"({existing.fingerprint} -> {fingerprint}，第 {self._drifts[name]} 次)；"
                "该段之后的前缀缓存全部作废。若这不是预期改动，"
                "检查是否有动态内容（时间戳/计数器）混进了稳定段"
            )
        self._static[name] = _StaticSegment(
            name=name, role=role, content=content, fingerprint=fingerprint
        )

    # ---------------------------------------------------------------- 尾部

    def volatile_msg(self, role: Role, content: str) -> None:
        """追加一条挥发性消息（每轮编译的内容，垫底）。"""
        self._tail.append(Message(role=role, content=content))

    def append(self, message: Message) -> None:
        """尾部追加一条增量消息（历史 / 工具结果 / 接力轮次）。"""
        self._tail.append(message)

    def extend(self, messages: list[Message]) -> None:
        """尾部批量追加增量消息。"""
        self._tail.extend(messages)

    def retract(self, n: int = 1) -> int:
        """从尾部静默弹掉 n 条消息（消息回退 / 撤回语义）。

        只脏尾部：截尾点之前的字节不变，前缀缓存继续命中。绝不插「撤回」
        注解（注解改字节 = 前缀报废）。n 超出尾部长度时夹到尾部边界并
        warning（撤回得比记得的多，调用方账本对不上，但不炸运行时）。

        Args:
            n: 要弹掉的消息条数

        Returns:
            int: 实际弹掉的条数
        """
        if n <= 0:
            return 0
        actual = min(n, len(self._tail))
        if actual < n:
            self._logger.warning(
                f"[{self._owner}] retract({n}) 超出尾部长度 {len(self._tail)}，"
                f"只弹了 {actual} 条（调用方的撤回账本与尾部不一致）"
            )
        if actual:
            del self._tail[-actual:]
        return actual

    def reset_tail(self) -> None:
        """清掉全部尾部内容，保留稳定段（每次调用重新编译尾部时用）。"""
        self._tail.clear()

    # ---------------------------------------------------------------- 渲染

    def render(self) -> list[Message]:
        """渲染最终消息列表：稳定段（登记序）+ 尾部（追加序）。"""
        return [
            Message(role=seg.role, content=seg.content) for seg in self._static.values()
        ] + list(self._tail)

    def fingerprint(self) -> str:
        """当前前缀指纹（段名:短hash 拼接，日志回放「第几轮开始漂的」用）。"""
        parts = [f"{seg.name}:{seg.fingerprint}" for seg in self._static.values()]
        return " ".join(parts) if parts else "(no-static)"

    # ---------------------------------------------------------------- 遥测

    def record_usage(self, usage: Usage) -> None:
        """记一次调用的 token 用量（cached/prompt 口径，算命中率）。

        Args:
            usage: 一次模型调用的 token 用量（Usage.cached_tokens 由
                provider 归一化解析，见 models/base.py::_parse_usage）
        """
        self._calls += 1
        self._prompt_tokens += usage.prompt_tokens
        self._cached_tokens += usage.cached_tokens
        if self._log_every > 0 and self._calls % self._log_every == 0:
            stats = self.stats()
            self._logger.info(
                f"[{self._owner}] 前缀缓存命中率: {stats.hit_rate:.0%} "
                f"({stats.cached_tokens}/{stats.prompt_tokens} tok, {stats.calls} 次调用)"
                + (f" 漂移: {stats.drifts}" if stats.drifts else "")
            )

    def stats(self) -> CacheStats:
        """命中率遥测快照（含各稳定段的漂移计数）。"""
        return CacheStats(
            calls=self._calls,
            prompt_tokens=self._prompt_tokens,
            cached_tokens=self._cached_tokens,
            drifts=dict(self._drifts),
        )

    # ---------------------------------------------------------------- 只读协议

    def __len__(self) -> int:
        """渲染后的消息条数。"""
        return len(self._static) + len(self._tail)

    def __iter__(self) -> Iterator[Message]:
        """按渲染序遍历消息（只读）。"""
        return iter(self.render())

    def __repr__(self) -> str:
        return (
            f"PromptCache(owner={self._owner!r}, static={list(self._static)}, "
            f"tail={len(self._tail)})"
        )

""" VirtualSession / SessionManager —— 上下文隔离层（架构文档 §6.2）"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from ..schemas.model_schema import CompletionResult, Message, ToolSpec
from ..utils.logger import LoggerManager
from ..utils.token_counter import DefaultCounter

class ChatBackend(Protocol):
    """ core 对模型能力的最小视图（L2 models 实现它，core 不反向依赖 models）"""

    async def chat(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult: ...

@dataclass(slots=True)
class VirtualSession:
    """ 一个 owner 专属的虚拟会话（独立消息历史 + 独立预算）"""
    session_id: str
    """ 会话唯一标识 """
    owner: str
    """ 归属者，如 "brain.think" / "brain.ooc" / "responder" """
    messages: list[Message] = field(default_factory=list)
    """ 这个会话自己的消息历史 """
    max_tokens: int = 8192
    """ 这个会话的上下文预算 """
    created_at: float = 0.0
    """ 创建时间 """
    last_used_at: float = 0.0
    """ 最近使用时间 """

class SessionManager:
    """ 一个模型实例 + 多个虚拟会话，框架层统一裁剪上下文，模块不碰上下文管理。

    call() 两种模式（架构文档 §6.2 / §9.3）：
    - stateless=True：用完即弃，不落会话历史（Brain 子模块的临时推理空间）
    - stateless=False：累积进 owner 会话并按预算裁剪（Responder 滑动窗口）
    """

    def __init__(self, backend: ChatBackend) -> None:
        self._logger = LoggerManager.get_logger("SESSION")
        self._backend = backend
        self._counter = DefaultCounter()
        self._sessions: dict[str, VirtualSession] = {}

    async def call(
        self,
        owner: str,
        messages: list[Message],
        *,
        stateless: bool = False,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult:
        """ 模块统一调用入口：拼会话 -> 预算裁剪 -> 调模型 -> 回写历史。

        Args:
            owner: 归属者标识（会话键 + 监控归因）
            messages: 本次追加的消息（stateless 时即为完整上下文）
            stateless: True 则用完即弃，不落会话历史
            max_new_tokens: 生成预留 token，同时用于上下文预算裁剪
            temperature: 采样温度
            stop: 停止序列
            tools: 工具声明（透传后端）

        Returns:
            CompletionResult: 模型补全结果

        Raises:
            后端模型的异常原样上抛，由调用方决定降级策略
        """
        started = time.monotonic()
        if stateless:
            self._logger.debug(
                f"调用: owner={owner} 模式=无状态 消息数={len(messages)} "
                f"max_new={max_new_tokens} temp={temperature}"
            )
            result = await self._backend.chat(
                messages, max_new_tokens=max_new_tokens,
                temperature=temperature, stop=stop, tools=tools,
            )
            self._logger.info(
                f"调用完成: owner={owner} 模式=无状态 耗时={time.monotonic() - started:.2f}s"
            )
            return result

        session = self._get_or_create(owner)
        history_before = len(session.messages)
        session.messages.extend(messages)
        session.messages = self._trim(session.messages, session.max_tokens - max_new_tokens)
        result = await self._backend.chat(
            session.messages, max_new_tokens=max_new_tokens,
            temperature=temperature, stop=stop, tools=tools,
        )
        if result.content:
            session.messages.append(Message(role="assistant", content=result.content))
        session.last_used_at = time.time()
        self._logger.info(
            f"调用完成: owner={owner} 模式=有状态 历史={history_before}->{len(session.messages)}条 "
            f"占用≈{self.usage(owner)}tok 耗时={time.monotonic() - started:.2f}s"
        )
        return result

    def set_system(self, owner: str, content: str) -> None:
        """ 设置 owner 会话的固定 system 前缀。

        前缀稳定且置于最前，有利于本地推理的 KV cache 前缀复用。
        """
        session = self._get_or_create(owner)
        session.messages = [m for m in session.messages if m.role != "system"]
        session.messages.insert(0, Message(role="system", content=content))

    def reset(self, owner: str) -> None:
        """ 清空某个 owner 的会话 """
        self._sessions.pop(owner, None)

    def reset_all(self) -> None:
        """ 清空全部会话（如新一轮对话开始）"""
        self._sessions.clear()

    def usage(self, owner: str) -> int:
        """ 该 owner 当前消息历史的 token 占用（供 HealthCenter 采集）"""
        session = self._sessions.get(owner)
        if not session:
            return 0
        return sum(self._counter.count(m.content) for m in session.messages)

    def _get_or_create(self, owner: str) -> VirtualSession:
        """ 取会话，不存在则创建 """
        if owner not in self._sessions:
            now = time.time()
            self._sessions[owner] = VirtualSession(
                session_id=f"{owner}-{uuid.uuid4().hex[:8]}", owner=owner, created_at=now,
            )
            self._logger.debug(f"创建虚拟会话: {owner}")
        return self._sessions[owner]

    def _trim(self, messages: list[Message], budget: int) -> list[Message]:
        """ 按预算裁剪：system 前缀固定保留，其余最早先淘汰 """
        total = sum(self._counter.count(m.content) for m in messages)
        if total <= budget:
            return messages

        system = [m for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]
        sys_tokens = sum(self._counter.count(m.content) for m in system)
        while rest and total > budget:
            total -= self._counter.count(rest[0].content)
            rest.pop(0)
        self._logger.debug(f"上下文裁剪: {len(messages)} -> {len(system) + len(rest)} 条")
        return system + rest

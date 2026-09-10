"""VirtualSession / SessionManager —— 上下文隔离层（架构文档 §6.2）"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from ..schemas.model_schema import CompletionResult, Message, StreamEvent, ToolSpec
from ..utils.logger import LoggerManager
from ..utils.token_counter import DefaultCounter


class ChatBackend(Protocol):
    """core 对模型能力的最小视图（L2 models 实现它，core 不反向依赖 models）"""

    async def chat(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult: ...

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """标准化工具调用格式（由 Provider 实现）"""
        ...


@dataclass(slots=True)
class VirtualSession:
    """一个 owner 专属的虚拟会话（独立消息历史 + 独立预算）"""

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
    """
    一个模型实例 + 多个虚拟会话，框架层统一裁剪上下文，模块不碰上下文管理。

    多模型路由：每个 owner 可以绑定不同的 ChatBackend，
    Brain 用 DeepSeek，Responder 用 Kimi，OOC 用本地小模型等。
    """

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("SESSION")
        self._counter = DefaultCounter()
        self._sessions: dict[str, VirtualSession] = {}
        self._backends: dict[str, ChatBackend] = {}
        self._default_backend: ChatBackend | None = None
        """ 默认 backend，未指定 owner 时使用 """

    def register_backend(self, owner: str, backend: ChatBackend) -> None:
        """注册某个 owner 的专属 backend。

        Args:
            owner: 模块标识，如 "brain.think" / "responder"
            backend: 对应模型的 ChatBackend 实现
        """
        if owner in self._backends:
            self._logger.warning(f"覆盖已注册的 backend: {owner}")
        self._backends[owner] = backend
        self._logger.info(f"注册 backend: {owner} -> {type(backend).__name__}")

    def set_default_backend(self, backend: ChatBackend) -> None:
        """设置默认 backend（未指定 owner 时使用）"""
        self._default_backend = backend
        self._logger.info(f"设置默认 backend: {type(backend).__name__}")

    def _get_backend(self, owner: str) -> ChatBackend:
        """获取 owner 对应的 backend，未注册则回退默认"""
        backend = self._backends.get(owner)
        if backend is None:
            backend = self._default_backend
            if backend is None:
                raise ValueError(f"未注册 backend 且无默认 backend: {owner}")
            self._logger.debug(f"owner={owner} 使用默认 backend")
        return backend

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
        """模块统一调用入口：按 owner 路由到对应 backend -> 拼会话 -> 预算裁剪 -> 调模型 -> 回写历史。"""
        started = time.monotonic()
        backend = self._get_backend(owner)

        if stateless:
            self._logger.debug(
                f"调用: owner={owner} 模式=无状态 消息数={len(messages)} "
                f"max_new={max_new_tokens} temp={temperature}"
            )
            result = await backend.chat(
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            )
            self._logger.info(
                f"调用完成: owner={owner} 模式=无状态 耗时={time.monotonic() - started:.2f}s"
            )
            return result

        session = self._get_or_create(owner)
        history_before = len(session.messages)
        session.messages.extend(messages)
        session.messages = self._trim(session.messages, session.max_tokens - max_new_tokens)
        result = await backend.chat(
            session.messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            tools=tools,
        )
        if result.content:
            session.messages.append(Message(role="assistant", content=result.content))
        session.last_used_at = time.time()
        self._logger.info(
            f"调用完成: owner={owner} 模式=有状态 历史={history_before}->{len(session.messages)}条 "
            f"占用≈{self.usage(owner)}tok 耗时={time.monotonic() - started:.2f}s"
        )
        return result

    async def call_stream(
        self,
        owner: str,
        messages: list[Message],
        *,
        stateless: bool = False,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ):
        """流式调用入口：按 owner 路由到 backend.chat_stream，逐块产出 StreamEvent。

        仅供投递层（给用户看的那一条）使用；内部模块（brain/ooc/压缩）仍走 `call()` 缓冲。
        后端若未实现 `chat_stream`，则回退到 `call()`/`chat()` 一次性产出（单块），保证兼容。

        Yields:
            StreamEvent: 正文块 + 末块（含 finish_reason / usage）
        """
        backend = self._get_backend(owner)
        stream_fn = getattr(backend, "chat_stream", None)

        if stream_fn is None:
            # 兜底：后端不支持流式，用 buffered chat() 一次性产出
            if stateless:
                result = await backend.chat(
                    messages,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    stop=stop,
                    tools=tools,
                )
            else:
                session = self._get_or_create(owner)
                session.messages.extend(messages)
                session.messages = self._trim(session.messages, session.max_tokens - max_new_tokens)
                result = await backend.chat(
                    session.messages,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    stop=stop,
                    tools=tools,
                )
                if result.content:
                    session.messages.append(Message(role="assistant", content=result.content))
                session.last_used_at = time.time()
            yield StreamEvent(
                delta=result.content, finish_reason=result.finish_reason, usage=result.usage
            )
            return

        if stateless:
            async for ev in stream_fn(
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            ):
                yield ev
            return

        session = self._get_or_create(owner)
        session.messages.extend(messages)
        session.messages = self._trim(session.messages, session.max_tokens - max_new_tokens)
        parts: list[str] = []
        async for ev in stream_fn(
            session.messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            tools=tools,
        ):
            if ev.delta:
                parts.append(ev.delta)
            yield ev
        content = "".join(parts)
        if content:
            session.messages.append(Message(role="assistant", content=content))
        session.last_used_at = time.time()

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """代理到后端 Provider 的工具调用标准化"""
        backend = self._get_backend(
            "normalize"
        )  # 用哪个 backend 无所谓，反正有 normalize_tool_calls
        if hasattr(backend, "normalize_tool_calls"):
            return backend.normalize_tool_calls(result, parsed_content)
        return result

    def export_messages(self, owner: str) -> list[Message]:
        """导出 owner 会话的完整消息历史（副本），供持久化层落盘。

        Args:
            owner: 模块标识

        Returns:
            list[Message]: 历史消息副本；无会话时返回空列表
        """
        session = self._sessions.get(owner)
        return list(session.messages) if session else []

    def import_messages(self, owner: str, messages: list[Message]) -> None:
        """导入消息历史，替换 owner 会话现有内容（重启恢复用）。

        Args:
            owner: 模块标识
            messages: 之前 export_messages 导出的消息列表
        """
        session = self._get_or_create(owner)
        session.messages = list(messages)
        session.last_used_at = time.time()
        self._logger.info(f"会话历史导入: owner={owner} {len(messages)} 条")

    def set_system(self, owner: str, content: str) -> None:
        """设置 owner 会话的固定 system 前缀。"""
        session = self._get_or_create(owner)
        session.messages = [m for m in session.messages if m.role != "system"]
        session.messages.insert(0, Message(role="system", content=content))

    def reset(self, owner: str) -> None:
        """清空某个 owner 的会话"""
        self._sessions.pop(owner, None)

    def reset_all(self) -> None:
        """清空全部会话（如新一轮对话开始）"""
        self._sessions.clear()

    def usage(self, owner: str) -> int:
        """该 owner 当前消息历史的 token 占用（供 HealthCenter 采集）"""
        session = self._sessions.get(owner)
        if not session:
            return 0
        return sum(self._counter.count(m.content) for m in session.messages)

    def _get_or_create(self, owner: str) -> VirtualSession:
        """取会话，不存在则创建"""
        if owner not in self._sessions:
            now = time.time()
            self._sessions[owner] = VirtualSession(
                session_id=f"{owner}-{uuid.uuid4().hex[:8]}",
                owner=owner,
                created_at=now,
            )
            self._logger.debug(f"创建虚拟会话: {owner}")
        return self._sessions[owner]

    def _trim(self, messages: list[Message], budget: int) -> list[Message]:
        """按预算裁剪：system 前缀固定保留，其余最早先淘汰"""
        total = sum(self._counter.count(m.content) for m in messages)
        if total <= budget:
            return messages

        system = [m for m in messages if m.role == "system"]
        rest = [m for m in messages if m.role != "system"]
        while rest and total > budget:
            total -= self._counter.count(rest[0].content)
            rest.pop(0)
        self._logger.debug(f"上下文裁剪: {len(messages)} -> {len(system) + len(rest)} 条")
        return system + rest

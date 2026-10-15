"""VirtualSession / SessionManager —— 上下文隔离层（架构文档 §6.2）"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from ..schemas.cost_schema import CostBreakdown
from ..schemas.model_schema import CompletionResult, Message, StreamEvent, ToolSpec, Usage
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
        execute_tools: bool = True,
    ) -> CompletionResult: ...

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """标准化工具调用格式（由 Provider 实现）"""
        ...

    def costs(self, usage: Usage, model: str = "") -> CostBreakdown:
        """计费查询：用量 -> 费用明细。

        协议约定：**未实现 costs() 的后端视为不计价**（本地 llama-server /
        测试假后端走这条路）——SessionManager 侧用 getattr 容差，实现方不必勉强。
        """
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

    作用域视图：`scoped(scope)` 派生一个共享路由（backend / 预算）、但会话表与
    用量账本各自独立的子管理器——多频道各拿一个，对话历史互不串味，单个
    频道 `reset_all()` 不波及他人；子视图的用量/费用同时汇总进父管理器。
    """

    def __init__(self, default_context_window: int = 8192) -> None:
        self._logger = LoggerManager.get_logger("SESSION")
        self._counter = DefaultCounter()
        self._sessions: dict[str, VirtualSession] = {}
        self._backends: dict[str, ChatBackend] = {}
        self._default_backend: ChatBackend | None = None
        """ 默认 backend，未指定 owner 时使用 """
        self._default_context_window = default_context_window
        """ 新建会话的默认上下文预算（装配层可用 set_context_window 逐 owner 覆盖）"""
        self._budgets: dict[str, int] = {}
        """ 逐 owner 的上下文预算（与会话对象解耦：reset 后新建的会话仍沿用）"""
        self._usage: dict[str, Usage] = {}
        """ 各 owner 的累计 token 用量（供健康监控计量）"""
        self._costs: dict[str, dict[str, float]] = {}
        """ 各 owner 的累计费用：owner -> 币种 -> 金额（本地/未收录模型为空） """
        self._parent: SessionManager | None = None
        """ 作用域视图的父管理器（路由与默认 backend 的来源、用量汇总的去处）"""
        self.scope = ""
        """ 作用域名（根管理器为空串）"""

    def scoped(self, scope: str) -> SessionManager:
        """派生一个作用域视图：共享 backend 路由与预算，会话与用量账本独立。

        Args:
            scope: 作用域名（如频道 ID）

        Returns:
            SessionManager: 子视图（其用量/费用同时累计进本管理器）
        """
        child = SessionManager(self._default_context_window)
        child._backends = self._backends
        child._budgets = self._budgets
        child._parent = self
        child.scope = scope
        return child

    def token_usage(self, owner: str) -> Usage:
        """读取某 owner 的累计 token 用量。

        Args:
            owner: 模块标识

        Returns:
            Usage: 该 owner 的累计用量（无记录则为零值）
        """
        return self._usage.get(owner, Usage())

    def total_usage(self) -> Usage:
        """读取全部 owner 的累计 token 用量（供回合计量取差值）。

        Returns:
            Usage: 累计用量合计（含缓存命中/缓存写入——llama-server 的
                前缀缓存与 Anthropic 的上下文缓存是成本观察的一手数据）
        """
        prompt = sum(u.prompt_tokens for u in self._usage.values())
        completion = sum(u.completion_tokens for u in self._usage.values())
        cached = sum(u.cached_tokens for u in self._usage.values())
        cache_write = sum(u.cache_write_tokens for u in self._usage.values())
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            cache_write_tokens=cache_write,
        )

    # ---------------------------------------------------------------- 计费

    def cost_by_owner(self, owner: str) -> dict[str, float]:
        """某 owner 的累计费用（币种 -> 金额；未计价模型为空 dict）。"""
        return dict(self._costs.get(owner, {}))

    def usage_breakdown(self) -> dict[str, Usage]:
        """全部 owner 的分模块累计用量（含只走无状态调用的 owner，如 gate.think）。

        Returns:
            dict: owner -> 累计用量（副本，供 /quota 这类报表逐模块展开）
        """
        return dict(self._usage)

    def total_cost(self) -> dict[str, float]:
        """全部 owner 的累计费用（币种 -> 金额；混合币种分开累计不混算）。"""
        totals: dict[str, float] = {}
        for per_currency in self._costs.values():
            for currency, amount in per_currency.items():
                totals[currency] = totals.get(currency, 0.0) + amount
        return totals

    def _accumulate_cost(self, owner: str, backend: ChatBackend, result: CompletionResult) -> None:
        """累计一次调用的费用（backend 未实现 costs() = 不计价，静默跳过）。"""
        pricing = getattr(backend, "costs", None)
        if not callable(pricing):
            return
        breakdown = pricing(result.usage, result.model)
        if not breakdown.priced:
            return
        manager: SessionManager | None = self
        while manager is not None:
            per_currency = manager._costs.setdefault(owner, {})
            per_currency[breakdown.currency] = (
                per_currency.get(breakdown.currency, 0.0) + breakdown.total
            )
            manager = manager._parent

    def owners(self) -> list[str]:
        """当前存在的虚拟会话 owner 列表。"""
        return list(self._sessions)

    def context_usage(self, owner: str) -> float:
        """该 owner 会话的上下文占用率（0.0 ~ 1.0+）。

        Args:
            owner: 模块标识

        Returns:
            float: 已用 token / 该会话预算；无会话时为 0.0
        """
        session = self._sessions.get(owner)
        if session is None or session.max_tokens <= 0:
            return 0.0
        return self.usage(owner) / session.max_tokens

    def _accumulate(self, owner: str, usage: Usage | None) -> None:
        """累计一次调用的 token 用量（含缓存命中/写入——前缀缓存复用率
        是成本观察的一手数据，丢了它 /quota 的缓存命中永远是 0）。"""
        if usage is None:
            return
        manager: SessionManager | None = self
        while manager is not None:
            current = manager._usage.get(owner, Usage())
            manager._usage[owner] = Usage(
                prompt_tokens=current.prompt_tokens + usage.prompt_tokens,
                completion_tokens=current.completion_tokens + usage.completion_tokens,
                cached_tokens=current.cached_tokens + usage.cached_tokens,
                cache_write_tokens=current.cache_write_tokens + usage.cache_write_tokens,
            )
            manager = manager._parent

    def register_backend(
        self, owner: str, backend: ChatBackend, *, context_window: int | None = None
    ) -> None:
        """注册某个 owner 的专属 backend。

        Args:
            owner: 模块标识，如 "brain.think" / "responder"
            backend: 对应模型的 ChatBackend 实现
            context_window: 该 owner 的上下文预算（None 表示沿用默认）
        """
        if owner in self._backends:
            self._logger.warning(f"覆盖已注册的 backend: {owner}")
        self._backends[owner] = backend
        if context_window is not None:
            self.set_context_window(owner, context_window)
        self._logger.info(f"注册 backend: {owner} -> {type(backend).__name__}")

    def set_default_backend(self, backend: ChatBackend) -> None:
        """设置默认 backend（未指定 owner 时使用）"""
        self._default_backend = backend
        self._logger.info(f"设置默认 backend: {type(backend).__name__}")

    def _fallback_backend(self) -> ChatBackend | None:
        """默认 backend：本管理器未设置时沿父链向上找。"""
        if self._default_backend is not None or self._parent is None:
            return self._default_backend
        return self._parent._fallback_backend()

    def _get_backend(self, owner: str) -> ChatBackend:
        """获取 owner 对应的 backend：精确匹配 -> 按点号逐级回退父 owner -> 默认。

        `brain.think.<步骤名>` / `brain.think.conclusion` 是 brain 的思考步骤，
        应落到 `brain.think` 注册的模型（此前精确匹配不中直接掉到默认模型，
        「Brain 用 DeepSeek」对卡片思考链形同虚设）。
        """
        backend = self._backends.get(owner)
        parent = owner
        while backend is None and "." in parent:
            parent = parent.rsplit(".", 1)[0]
            backend = self._backends.get(parent)
        if backend is None:
            backend = self._fallback_backend()
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
        execute_tools: bool = True,
    ) -> CompletionResult:
        """模块统一调用入口：按 owner 路由到对应 backend -> 拼会话 -> 预算裁剪 -> 调模型 -> 回写历史。

        有状态调用**成功后才提交**本轮消息：模型调用失败（网络 / 配额 / 超时 / 取消）
        时历史原样不动——此前先追加再调用，失败后下一回合会带着两条连续的 user
        消息（外加残留的「继续…」/ 过渡语指令）去问模型。
        """
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
                execute_tools=execute_tools,
            )
            self._accumulate(owner, result.usage)
            self._accumulate_cost(owner, backend, result)
            self._logger.info(
                f"调用完成: owner={owner} 模式=无状态 耗时={time.monotonic() - started:.2f}s"
            )
            return result

        session = self._get_or_create(owner)
        history_before = len(session.messages)
        pending = self._trim([*session.messages, *messages], session.max_tokens - max_new_tokens)
        result = await backend.chat(
            pending,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            tools=tools,
            execute_tools=execute_tools,
        )
        self._accumulate(owner, result.usage)
        self._accumulate_cost(owner, backend, result)
        self._commit(session, pending, result.content)
        self._logger.info(
            f"调用完成: owner={owner} 模式=有状态 历史={history_before}->{len(session.messages)}条 "
            f"占用≈{self.usage(owner)}tok 耗时={time.monotonic() - started:.2f}s"
        )
        return result

    async def native_tools_supported(self, owner: str) -> bool:
        """该 owner 路由到的模型是否支持原生 tool_calls（探测结果按 provider 实例缓存）。

        鸭子类型后端（测试假后端 / 第三方极简实现）没有探测方法时按不支持处理，
        落文本喊话备胎——与旧行为一致。
        """
        backend = self._get_backend(owner)
        probe = getattr(backend, "probe_native_tools", None)
        if probe is None:
            return False
        return await probe()

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

        有状态流式：已经流出去的文本就是用户看到的话——中途出错或消费方提前停止，
        也把「本轮输入 + 已产出的半截回复」提交进历史；一个字都没产出则历史不动。

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
                pending = self._trim(
                    [*session.messages, *messages], session.max_tokens - max_new_tokens
                )
                result = await backend.chat(
                    pending,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    stop=stop,
                    tools=tools,
                )
                self._commit(session, pending, result.content)
            self._accumulate(owner, result.usage)
            self._accumulate_cost(owner, backend, result)
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
                if ev.usage is not None:
                    self._accumulate(owner, ev.usage)
                    self._accumulate_cost(owner, backend, CompletionResult(usage=ev.usage))
                yield ev
            return

        session = self._get_or_create(owner)
        pending = self._trim([*session.messages, *messages], session.max_tokens - max_new_tokens)
        parts: list[str] = []
        try:
            async for ev in stream_fn(
                pending,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            ):
                if ev.delta:
                    parts.append(ev.delta)
                if ev.usage is not None:
                    self._accumulate(owner, ev.usage)
                    self._accumulate_cost(owner, backend, CompletionResult(usage=ev.usage))
                yield ev
        finally:
            if parts:
                self._commit(session, pending, "".join(parts))

    def _commit(self, session: VirtualSession, pending: list[Message], reply: str) -> None:
        """把一次成功调用的上下文（已裁剪的历史 + 本轮输入）与回复写回会话。

        Args:
            session: 目标会话
            pending: 本次实际发给模型的消息
            reply: 模型回复（空串不入历史）
        """
        session.messages = (
            [*pending, Message(role="assistant", content=reply)] if reply else pending
        )
        session.last_used_at = time.time()

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None, *, owner: str = ""
    ) -> CompletionResult:
        """代理到产出该结果的 owner 所路由的 Provider 做工具调用标准化。

        Args:
            result: 模型输出
            parsed_content: 正文解析出的 JSON（喊话抢救用）
            owner: 产出 result 的模块标识（不同 provider 的喊话格式不同，
                要交给同一个 provider 认；空串 = 默认 backend）

        Returns:
            CompletionResult: tool_calls 已标准化的结果
        """
        backend = self._get_backend(owner) if owner else self._fallback_backend()
        if backend is not None and hasattr(backend, "normalize_tool_calls"):
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

    def set_context_window(self, owner: str, tokens: int) -> None:
        """设置某 owner 会话的上下文预算（装配层按模型配置下发）。

        Args:
            owner: 模块标识
            tokens: 预算 token 数；<= 0 时忽略
        """
        if tokens <= 0:
            return
        self._budgets[owner] = tokens
        self._get_or_create(owner).max_tokens = tokens
        self._logger.debug(f"会话预算设置: owner={owner} max_tokens={tokens}")

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
        """取会话，不存在则创建（用管理器默认的上下文预算）"""
        if owner not in self._sessions:
            now = time.time()
            budget = self._budgets.get(owner, self._default_context_window)
            self._sessions[owner] = VirtualSession(
                session_id=f"{owner}-{uuid.uuid4().hex[:8]}",
                owner=owner,
                max_tokens=budget,
                created_at=now,
            )
            self._logger.debug(f"创建虚拟会话: {owner} 预算={budget}tok")
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

"""资源闸门与限流 —— 保护单模型这一份稀缺资源。

设计锚点：本地单模型是串行稀缺资源，「多路」的本质是**排队 + 路由**而非并行。
本模块提供三层限流，各管一段：

- `IngressLimiter`：入口令牌桶，按用户早拒（不占模型）
- `ResourceGate`：全局并发 + 每租户速率 / 每日调用 / 每日 token 配额
- `GatedBackend`：包住 `ChatBackend`，把每次模型调用汇入闸门（装配处一行接入）

租户归属靠 `tenant_scope()`（contextvars）：世界的整个 `start()` 任务包在
`tenant_scope(session_id)` 里，任务内所有模型调用就自动归到该租户，
**无需把身份穿透 SessionManager**。
"""

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field

import msgspec

from ..schemas.model_schema import CompletionResult, Message, StreamEvent, ToolSpec, Usage
from ..schemas.quota_schema import TenantQuota
from ..utils.logger import LoggerManager
from .session_manager import ChatBackend

_RATE_WINDOW = 60.0
""" 速率窗口：1 分钟 """

_DAY_SECONDS = 86400.0
""" 用量窗口：1 天 """


class QuotaExceeded(Exception):
    """超出配额（速率 / 用量）。调用方应据此回绝请求并给出等待时间。"""

    def __init__(self, tenant: str, reason: str, retry_after: float = 0.0) -> None:
        super().__init__(f"配额超限[{tenant}]: {reason}")
        self.tenant = tenant
        """ 触发超限的租户 """
        self.reason = reason
        """ 超限原因（人类可读）"""
        self.retry_after = max(0.0, retry_after)
        """ 建议等待秒数 """


_current_tenant: ContextVar[str] = ContextVar("gensokyoai_tenant", default="default")
""" 当前租户标识 —— 由入口/世界任务设置，闸门据此记账 """


def current_tenant() -> str:
    """读取当前上下文的租户标识。"""
    return _current_tenant.get()


@contextlib.contextmanager
def tenant_scope(tenant: str) -> Iterator[None]:
    """把作用域内的模型调用归属到指定租户。

    世界任务在启动时进入该作用域（`tenant_scope(session_id)`），
    于是任务内所有 await 链上的模型调用都自动算在这个租户头上。

    Args:
        tenant: 租户标识（如 session_id / channel_id）

    Yields:
        None
    """
    token = _current_tenant.set(tenant)
    try:
        yield
    finally:
        _current_tenant.reset(token)


@dataclass
class _TenantState:
    """单个租户的运行时状态。"""

    quota: TenantQuota
    semaphore: asyncio.Semaphore
    hits: deque[float] = field(default_factory=deque)
    """ 速率窗口内的请求时刻 """
    last_used_at: float = 0.0


class ResourceGate:
    """全局资源闸门：单模型串行 + 每租户配额 + 排队。

    所有模型调用经 `GatedBackend` 汇聚到这里：
    - **全局并发**受 `max_concurrent` 限制（本地单模型建议 1，避免并发打爆显存）
    - **每租户**受速率（rpm）、并发、每日调用 / token 预算限制
    - 超限抛 `QuotaExceeded`（带 `retry_after`），**不静默丢弃**

    并发等待由 `asyncio.Semaphore` 承担，其唤醒顺序为到达序，即近似先来先服务；
    配合每租户速率上限，可避免单个话痨租户饿死其他租户。
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 1,
        default_rpm: int = 0,
        default_concurrency: int = 1,
        default_calls_per_day: int = 0,
        default_tokens_per_day: int = 0,
        clock=time.time,
    ) -> None:
        """初始化闸门。

        Args:
            max_concurrent: 全局并发上限（本地单模型建议 1）
            default_rpm: 租户默认每分钟调用上限；0 不限
            default_concurrency: 租户默认并发上限
            default_calls_per_day: 租户默认每日调用上限；0 不限
            default_tokens_per_day: 租户默认每日 token 预算；0 不限
            clock: 时钟函数（可注入以便测试）
        """
        self._logger = LoggerManager.get_logger("RESOURCE")
        self._global = asyncio.Semaphore(max(1, max_concurrent))
        self._tenants: dict[str, _TenantState] = {}
        self._clock = clock
        self._defaults = TenantQuota(
            rpm=default_rpm,
            concurrency=default_concurrency,
            calls_per_day=default_calls_per_day,
            tokens_per_day=default_tokens_per_day,
        )

    def configure(self, tenant: str, quota: TenantQuota) -> None:
        """为租户设定配额（覆盖默认值；用量计数保留）。"""
        state = self._tenants.get(tenant)
        preset = msgspec.structs.replace(quota, tenant=tenant)
        if state is None:
            self._tenants[tenant] = _TenantState(
                quota=msgspec.structs.replace(preset, window_reset_at=self._clock() + _DAY_SECONDS),
                semaphore=asyncio.Semaphore(max(1, preset.concurrency)),
            )
            return
        state.quota = msgspec.structs.replace(
            preset,
            used_calls=state.quota.used_calls,
            used_tokens=state.quota.used_tokens,
            window_reset_at=state.quota.window_reset_at,
        )

    @contextlib.asynccontextmanager
    async def acquire(self, tenant: str) -> AsyncIterator[TenantQuota]:
        """申请一次模型调用额度：查速率/预算 -> 计数 -> 排队等并发。

        Args:
            tenant: 租户标识

        Yields:
            TenantQuota: 该租户的配额与用量快照

        Raises:
            QuotaExceeded: 速率或每日预算超限
        """
        state = self._state(tenant)
        now = self._clock()
        self._roll_window(state, now)
        quota = state.quota

        if quota.rpm > 0:
            while state.hits and now - state.hits[0] > _RATE_WINDOW:
                state.hits.popleft()
            if len(state.hits) >= quota.rpm:
                wait = _RATE_WINDOW - (now - state.hits[0])
                raise QuotaExceeded(tenant, f"速率超限（{quota.rpm}/分钟）", wait)

        if quota.calls_per_day > 0 and quota.used_calls >= quota.calls_per_day:
            raise QuotaExceeded(
                tenant,
                f"每日调用预算已用尽（{quota.calls_per_day}）",
                quota.window_reset_at - now,
            )
        if quota.tokens_per_day > 0 and quota.used_tokens >= quota.tokens_per_day:
            raise QuotaExceeded(
                tenant,
                f"每日 token 预算已用尽（{quota.tokens_per_day}）",
                quota.window_reset_at - now,
            )

        state.hits.append(now)
        state.last_used_at = now
        quota.used_calls += 1

        async with self._global, state.semaphore:
            yield quota

    def record(self, tenant: str, usage: Usage | None = None) -> None:
        """记一次调用的 token 用量（输入 + 输出）。

        Args:
            tenant: 租户标识
            usage: 该次调用的用量；None 时只记调用数（已在 acquire 中计）
        """
        if usage is None:
            return
        state = self._state(tenant)
        self._roll_window(state, self._clock())
        state.quota.used_tokens += usage.prompt_tokens + usage.completion_tokens

    def status(self, tenant: str) -> TenantQuota:
        """取某租户的配额快照（副本）。"""
        return msgspec.structs.replace(self._state(tenant).quota)

    def snapshot(self) -> list[TenantQuota]:
        """取全部租户的配额快照（供健康监控采集）。"""
        return [msgspec.structs.replace(s.quota) for s in self._tenants.values()]

    def _state(self, tenant: str) -> _TenantState:
        """取（或懒创建）租户状态。"""
        state = self._tenants.get(tenant)
        if state is None:
            state = _TenantState(
                quota=msgspec.structs.replace(
                    self._defaults,
                    tenant=tenant,
                    window_reset_at=self._clock() + _DAY_SECONDS,
                ),
                semaphore=asyncio.Semaphore(max(1, self._defaults.concurrency)),
            )
            self._tenants[tenant] = state
            self._logger.debug(f"创建租户配额: {tenant}")
        return state

    def _roll_window(self, state: _TenantState, now: float) -> None:
        """跨过窗口就重置每日用量计数。"""
        if now < state.quota.window_reset_at:
            return
        state.quota.used_calls = 0
        state.quota.used_tokens = 0
        state.quota.window_reset_at = now + _DAY_SECONDS


class IngressLimiter:
    """入口令牌桶：按用户限速，超速即拒（**不占模型资源**）。

    放在服务入口（WS/HTTP 处理器）而不是闸门里，因为「太频繁」应该在
    还没变成一次模型调用之前就被挡掉。
    """

    def __init__(self, *, rate: float, burst: int, clock=time.time) -> None:
        """初始化。

        Args:
            rate: 令牌补充速率（个/秒）
            burst: 桶容量（允许的突发条数）
            clock: 时钟函数（可注入以便测试）
        """
        self._rate = max(0.0, rate)
        self._burst = max(1, burst)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        """ user -> (剩余令牌, 上次刷新时刻) """

    def check(self, user: str) -> float:
        """尝试取一个令牌。

        Args:
            user: 用户标识

        Returns:
            float: 0.0 表示放行；> 0 表示被限速，值为建议等待秒数
        """
        if self._rate <= 0:
            return 0.0
        now = self._clock()
        tokens, last = self._buckets.get(user, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            self._buckets[user] = (tokens - 1.0, now)
            return 0.0
        self._buckets[user] = (tokens, now)
        return (1.0 - tokens) / self._rate


class GatedBackend:
    """把任意 `ChatBackend` 包一层资源闸门（装配处一行接入）。

    实现 `ChatBackend` 协议，因此对 `SessionManager` 完全透明：
    各模块照常调用，配额与并发在背后生效。
    """

    def __init__(self, inner: ChatBackend, gate: ResourceGate) -> None:
        """初始化。

        Args:
            inner: 被包装的后端（Provider）
            gate: 资源闸门
        """
        self._inner = inner
        self._gate = gate

    @property
    def supports_streaming(self) -> bool:
        """透传内层后端的流式能力。"""
        return bool(getattr(self._inner, "supports_streaming", False))

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """透传工具调用标准化。"""
        normalizer = getattr(self._inner, "normalize_tool_calls", None)
        if normalizer is None:
            return result
        return normalizer(result, parsed_content)

    async def chat(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult:
        """经闸门调用内层后端（缓冲路径）。"""
        tenant = current_tenant()
        async with self._gate.acquire(tenant):
            result = await self._inner.chat(
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            )
        self._gate.record(tenant, result.usage)
        return result

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """经闸门调用内层后端的流式接口；内层无流式时回退缓冲并单块产出。"""
        stream_fn = getattr(self._inner, "chat_stream", None)
        if stream_fn is None:
            result = await self.chat(
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            )
            yield StreamEvent(
                delta=result.content, finish_reason=result.finish_reason, usage=result.usage
            )
            return

        tenant = current_tenant()
        async with self._gate.acquire(tenant):
            async for event in stream_fn(
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                stop=stop,
                tools=tools,
            ):
                if event.usage is not None:
                    self._gate.record(tenant, event.usage)
                yield event

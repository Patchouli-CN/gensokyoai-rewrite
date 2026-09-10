"""资源闸门与限流单元测试：速率 / 用量预算 / 并发 / 租户归属"""

import asyncio

import pytest

from gensokyoai.core.resource import (
    GatedBackend,
    IngressLimiter,
    QuotaExceeded,
    ResourceGate,
    current_tenant,
    tenant_scope,
)
from gensokyoai.schemas.model_schema import CompletionResult, Message, Usage
from gensokyoai.schemas.quota_schema import TenantQuota


class _Clock:
    """可控时钟（秒），用于免 sleep 地测窗口推进。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _StubBackend:
    """最小 ChatBackend：无流式接口，用于验证 GatedBackend 的兜底路径。"""

    def __init__(self, content: str = "ok", completion_tokens: int = 10) -> None:
        self.content = content
        self.completion_tokens = completion_tokens
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        return CompletionResult(
            content=self.content,
            usage=Usage(prompt_tokens=3, completion_tokens=self.completion_tokens),
        )

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _msg() -> list[Message]:
    return [Message(role="user", content="hi")]


async def test_gate_counts_calls_and_records_tokens():
    """acquire 计调用数，record 累加 token"""
    gate = ResourceGate(clock=_Clock())
    async with gate.acquire("t1"):
        pass
    gate.record("t1", Usage(prompt_tokens=5, completion_tokens=7))
    status = gate.status("t1")
    assert status.used_calls == 1
    assert status.used_tokens == 12


async def test_gate_rate_limit_raises_with_retry_after():
    """超过每分钟调用上限抛 QuotaExceeded，并给出等待时间；窗口过后放行"""
    clock = _Clock()
    gate = ResourceGate(clock=clock)
    gate.configure("t1", TenantQuota(rpm=2))

    async with gate.acquire("t1"):
        pass
    async with gate.acquire("t1"):
        pass
    with pytest.raises(QuotaExceeded) as exc:
        async with gate.acquire("t1"):
            pass
    assert exc.value.retry_after > 0
    assert exc.value.tenant == "t1"

    clock.advance(61)
    async with gate.acquire("t1"):
        pass


async def test_gate_daily_call_budget_resets_after_window():
    """每日调用预算用尽后拒绝，跨窗口自动重置"""
    clock = _Clock()
    gate = ResourceGate(clock=clock)
    gate.configure("d", TenantQuota(calls_per_day=1))

    async with gate.acquire("d"):
        pass
    with pytest.raises(QuotaExceeded):
        async with gate.acquire("d"):
            pass

    clock.advance(86401)
    async with gate.acquire("d"):
        pass


async def test_gate_daily_token_budget():
    """每日 token 预算用尽后拒绝"""
    gate = ResourceGate(clock=_Clock())
    gate.configure("d", TenantQuota(tokens_per_day=10))
    async with gate.acquire("d"):
        pass
    gate.record("d", Usage(prompt_tokens=6, completion_tokens=6))
    with pytest.raises(QuotaExceeded):
        async with gate.acquire("d"):
            pass


async def test_rejected_call_is_not_counted():
    """被拒的请求不计数（不会雪上加霜）"""
    gate = ResourceGate(clock=_Clock())
    gate.configure("t", TenantQuota(calls_per_day=1))
    async with gate.acquire("t"):
        pass
    with pytest.raises(QuotaExceeded):
        async with gate.acquire("t"):
            pass
    assert gate.status("t").used_calls == 1


async def test_gate_global_concurrency_serializes():
    """全局并发上限为 1 时，模型调用严格串行（保护单模型）"""
    gate = ResourceGate(max_concurrent=1)
    active = 0
    peak = 0

    async def worker(n: int) -> None:
        nonlocal active, peak
        async with gate.acquire(f"t{n}"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker(i) for i in range(3)))
    assert peak == 1


async def test_gated_backend_records_usage_to_tenant():
    """GatedBackend 把调用记到 tenant_scope 指定的租户头上"""
    gate = ResourceGate()
    gated = GatedBackend(_StubBackend(completion_tokens=7), gate)

    with tenant_scope("chan-1"):
        result = await gated.chat(_msg(), max_new_tokens=8)

    assert result.content == "ok"
    status = gate.status("chan-1")
    assert status.used_calls == 1
    assert status.used_tokens == 3 + 7
    assert gate.status("default").used_calls == 0


async def test_gated_backend_stream_falls_back_without_inner_stream():
    """内层无 chat_stream 时，GatedBackend 回退缓冲并单块产出，且照常记账"""
    gate = ResourceGate()
    gated = GatedBackend(_StubBackend(content="整段回复"), gate)

    with tenant_scope("chan-2"):
        events = [ev async for ev in gated.chat_stream(_msg())]

    assert events[0].delta == "整段回复"
    assert events[0].finish_reason == "stop"
    assert gate.status("chan-2").used_tokens == 3 + 10


async def test_gated_backend_supports_streaming_passthrough():
    """supports_streaming 透传内层能力（内层没有则 False）"""

    class _StreamingStub(_StubBackend):
        @property
        def supports_streaming(self) -> bool:
            return True

    gate = ResourceGate()
    assert GatedBackend(_StubBackend(), gate).supports_streaming is False
    assert GatedBackend(_StreamingStub(), gate).supports_streaming is True


def test_ingress_limiter_allows_burst_then_throttles():
    """入口令牌桶：允许突发，超速返回等待秒数，补充后恢复"""
    clock = _Clock()
    limiter = IngressLimiter(rate=1.0, burst=2, clock=clock)

    assert limiter.check("u") == 0.0
    assert limiter.check("u") == 0.0
    wait = limiter.check("u")
    assert wait > 0

    clock.advance(1.0)
    assert limiter.check("u") == 0.0


def test_ingress_limiter_disabled_when_rate_zero():
    """速率为 0 表示关闭限流，永远放行"""
    limiter = IngressLimiter(rate=0.0, burst=1)
    assert all(limiter.check("u") == 0.0 for _ in range(10))


def test_tenant_scope_sets_and_restores():
    """tenant_scope 进作用域生效、出作用域还原"""
    assert current_tenant() == "default"
    with tenant_scope("x"):
        assert current_tenant() == "x"
    assert current_tenant() == "default"


async def test_gate_snapshot_lists_all_tenants():
    """snapshot 汇总所有租户（供健康监控采集）"""
    gate = ResourceGate()
    async with gate.acquire("a"):
        pass
    async with gate.acquire("b"):
        pass
    tenants = {q.tenant for q in gate.snapshot()}
    assert tenants == {"a", "b"}

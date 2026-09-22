"""模型计费兼容层测试：四条流数学 / 分档 / 多币种 / 未知模型 / Provider.costs / 累计"""

import pytest

from gensokyoai.core.resource import GatedBackend, ResourceGate
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.models.base import OpenAICompatProvider
from gensokyoai.models.pricing import price_for
from gensokyoai.schemas.cost_schema import ModelPrice, PriceTier, cost_of
from gensokyoai.schemas.model_schema import CompletionResult, Message, ModelConfig, Usage

_LUNA = price_for("gpt-5.6-luna")
""" $0.2 输入 / $1.2 输出 / $0.02 缓存读 """


# ---------- 纯计算 ----------


def test_cost_of_flat_four_streams():
    """四条流各算各的：普通输入 / 缓存读 / 缓存写 / 输出"""
    breakdown = cost_of(
        prompt_tokens=1000,
        completion_tokens=200,
        cached_tokens=100,
        cache_write_tokens=50,
        price=_LUNA,
        model="gpt-5.6-luna",
    )
    # 普通输入 = 1000 - 100(读) - 50(写) = 850；费用 = (850×0.2 + 100×0.02 + 50×0 + 200×1.2)/1M
    assert breakdown.priced is True
    assert breakdown.input_tokens == 850, "普通输入 = prompt - cached - cache_write"
    assert breakdown.currency == "USD"
    assert breakdown.total == pytest.approx((850 * 0.2 + 100 * 0.02 + 200 * 1.2) / 1_000_000)
    assert breakdown.cache_write_cost == 0.0, "OpenAI 无缓存写计费"


def test_cost_of_cache_write_stream():
    """Anthropic 的缓存写是真实计费流（1.25× 输入）；且不重复计入普通输入"""
    opus = price_for("claude-opus-5")
    breakdown = cost_of(
        prompt_tokens=1_000_000,
        completion_tokens=0,
        cached_tokens=0,
        cache_write_tokens=200_000,
        price=opus,
    )
    # 800K 普通输入 × $5 + 200K 缓存写 × $6.25（写入口也从总量里扣除）
    assert breakdown.input_tokens == 800_000
    assert breakdown.total == pytest.approx(800_000 * 5 / 1_000_000 + 200_000 * 6.25 / 1_000_000)


def test_cost_of_three_way_input_split():
    """三种输入流相加恰等于总量：普通 + 缓存读 + 缓存写 = prompt"""
    breakdown = cost_of(
        prompt_tokens=10_000,
        completion_tokens=0,
        cached_tokens=7_000,
        cache_write_tokens=2_000,
        price=price_for("claude-sonnet-5"),
    )
    assert breakdown.input_tokens + breakdown.cached_tokens + breakdown.cache_write_tokens == 10_000


def test_cost_of_tiered_by_input_size():
    """分档：qwen3-max 按单次输入量切价（100K 输入落第二档 $2.4/$12）"""
    qwen_max = price_for("qwen3-max")
    assert qwen_max is not None and len(qwen_max.tiers) == 3
    small = cost_of(prompt_tokens=10_000, completion_tokens=1000, price=qwen_max)
    large = cost_of(prompt_tokens=100_000, completion_tokens=1000, price=qwen_max)
    assert small.total == pytest.approx((10_000 * 1.2 + 1000 * 6.0) / 1_000_000)
    assert large.total == pytest.approx((100_000 * 2.4 + 1000 * 12.0) / 1_000_000)


def test_cost_of_tier_boundaries():
    """档位边界：<=32K 落第一档（含边界），32K+1 落第二档"""
    qwen_max = price_for("qwen3-max")
    at_edge = cost_of(prompt_tokens=32_000, completion_tokens=0, price=qwen_max)
    over_edge = cost_of(prompt_tokens=32_001, completion_tokens=0, price=qwen_max)
    assert at_edge.total == pytest.approx(32_000 * 1.2 / 1_000_000)
    assert over_edge.total == pytest.approx(32_001 * 2.4 / 1_000_000)


def test_cost_of_unpriced_is_honest_zero():
    """未收录/本地模型：priced=False、费用 0，但 token 数照实记（不是「免费」）"""
    breakdown = cost_of(
        prompt_tokens=1500, completion_tokens=300, price=None, model="my-local-qwen"
    )
    assert breakdown.priced is False
    assert breakdown.total == 0.0
    assert breakdown.input_tokens == 1500 and breakdown.output_tokens == 300
    assert breakdown.model == "my-local-qwen"


def test_cost_of_currency_passthrough():
    """币种跟着价格表走（CNY 表记 CNY，不和 USD 混算）"""
    cny = ModelPrice(currency="CNY", tiers=(PriceTier(price_in=1.0, price_out=2.0),))
    breakdown = cost_of(prompt_tokens=1_000_000, completion_tokens=0, price=cny)
    assert breakdown.currency == "CNY"
    assert breakdown.total == pytest.approx(1.0)


# ---------- 内置价格表 ----------


def test_price_for_known_and_unknown():
    """收录的查得到；未收录返回 None（调用方按 unpriced 处理）"""
    assert price_for("gpt-5.6-sol") is not None
    assert price_for("deepseek-flash") is not None
    assert price_for("qwen3-max") is not None
    assert price_for("claude-sonnet-5") is not None
    assert price_for("totally-unknown-model-9000") is None


def test_price_for_normalizes_case_and_provider_prefix():
    """大小写不敏感；剥掉网关常见的 `提供商/` 前缀"""
    assert price_for("GPT-5.6-Luna") is price_for("gpt-5.6-luna")
    assert price_for("anthropic/claude-sonnet-5") is price_for("claude-sonnet-5")


def test_builtin_price_sanity():
    """内置表抽查：输出 >= 输入、缓存读 <= 输入（防手抄错）"""
    for name in ("gpt-5.6-sol", "claude-opus-5", "deepseek-flash", "gemini-3.6-flash"):
        price = price_for(name)
        assert price is not None
        tier = price.tiers[0]
        assert tier.price_out >= tier.price_in, name
        assert tier.price_cached_in <= tier.price_in, name


# ---------- Provider.costs() ----------


def _provider(**overrides) -> OpenAICompatProvider:
    provider = OpenAICompatProvider()
    provider.config(ModelConfig(**overrides))
    return provider


def test_provider_costs_uses_builtin_table():
    """未配价时按响应/配置模型名查内置表"""
    provider = _provider(model_name="gpt-5.6-luna")
    breakdown = provider.costs(Usage(prompt_tokens=1000, completion_tokens=100), "gpt-5.6-luna")
    assert breakdown.priced is True
    assert breakdown.total == pytest.approx((1000 * 0.2 + 100 * 1.2) / 1_000_000)


def test_provider_costs_config_override_wins():
    """配置价优先于内置表（新模型/精确价走配置）"""
    custom = ModelPrice(
        currency="CNY",
        tiers=(PriceTier(price_in=1.0, price_out=2.0, price_cached_in=0.1),),
    )
    provider = _provider(model_name="gpt-5.6-luna", price=custom)
    breakdown = provider.costs(Usage(prompt_tokens=1_000_000, completion_tokens=0))
    assert breakdown.currency == "CNY"
    assert breakdown.total == pytest.approx(1.0), "配置价必须压过内置表"


def test_provider_costs_local_is_unpriced():
    """本地 llama-server 没配价 = 不计价（电费不在 token 口径内）"""
    provider = _provider(model_name="qwen")
    breakdown = provider.costs(Usage(prompt_tokens=1500, completion_tokens=200), "qwen")
    assert breakdown.priced is False
    assert breakdown.total == 0.0


# ---------- GatedBackend 透传 ----------


def test_gated_backend_delegates_costs():
    """GatedBackend.costs 透传给内层 provider"""
    inner = _provider(model_name="gpt-5.6-luna")
    gated = GatedBackend(inner, ResourceGate())
    breakdown = gated.costs(Usage(prompt_tokens=1000, completion_tokens=100), "gpt-5.6-luna")
    assert breakdown.priced is True


def test_gated_backend_without_costs_is_unpriced():
    """内层没有 costs（旧后端）→ unpriced，不炸"""

    class _LegacyBackend:
        async def chat(self, messages, **kwargs):
            return CompletionResult(content="hi")

        def normalize_tool_calls(self, result, parsed_content=None):
            return result

    gated = GatedBackend(_LegacyBackend(), ResourceGate())
    breakdown = gated.costs(Usage(prompt_tokens=100, completion_tokens=10))
    assert breakdown.priced is False


# ---------- SessionManager 累计 ----------


class _PricedBackend:
    """实现了 costs() 的假后端（按 gpt-5.6-luna 计价）"""

    async def chat(self, messages, **kwargs):
        return CompletionResult(
            content="hi",
            usage=Usage(prompt_tokens=1000, completion_tokens=100),
            model="gpt-5.6-luna",
        )

    def normalize_tool_calls(self, result, parsed_content=None):
        return result

    def costs(self, usage: Usage, model: str = ""):
        return cost_of(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            price=price_for(model),
            model=model,
        )


class _PricelessBackend:
    """没有 costs() 的假后端（协议容差路径）"""

    async def chat(self, messages, **kwargs):
        return CompletionResult(
            content="hi", usage=Usage(prompt_tokens=1000, completion_tokens=100)
        )

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


async def test_session_accumulates_cost_per_owner():
    """按 owner 累计费用；多次调用累加"""
    sessions = SessionManager()
    sessions.set_default_backend(_PricedBackend())
    messages = [Message(role="user", content="x")]
    await sessions.call("brain.think", messages, stateless=True, max_new_tokens=16)
    await sessions.call("responder", messages, stateless=True, max_new_tokens=16)
    await sessions.call("brain.think", messages, stateless=True, max_new_tokens=16)

    per_call = (1000 * 0.2 + 100 * 1.2) / 1_000_000
    assert sessions.cost_by_owner("brain.think") == {"USD": pytest.approx(per_call * 2)}
    assert sessions.cost_by_owner("responder") == {"USD": pytest.approx(per_call)}
    assert sessions.total_cost() == {"USD": pytest.approx(per_call * 3)}


async def test_session_without_costs_capability_is_silent():
    """后端没实现 costs()：不炸、不计价、总量为空"""
    sessions = SessionManager()
    sessions.set_default_backend(_PricelessBackend())
    await sessions.call("brain.think", [Message(role="user", content="x")], stateless=True)
    assert sessions.total_cost() == {}
    assert sessions.cost_by_owner("brain.think") == {}

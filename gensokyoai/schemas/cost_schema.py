"""模型计费数据契约与纯计算 —— 兼容各家「四条计费流」的最大公约数。

调研（2026-09）确认各家计费都落在四条流上，差异只在**有没有缓存写**与**是否分档**：

| 流 | 说明 | 谁有 |
|---|---|---|
| 普通输入 | 命中的 prompt token（不含缓存部分） | 全部 |
| 缓存输入（读） | 命中上下文缓存的 prompt token，通常 1 折 | 全部 |
| 缓存输入（写） | 把前缀写入缓存的 token，通常 1.25× 输入 | Anthropic / Kimi / 阿里 |
| 输出 | completion（含思维链 token） | 全部 |

另有**输入分档**（按单次请求输入 token 数切价，Qwen max/plus、Gemini 长上下文）
与**分时计价**（DeepSeek 峰谷、阿里夜间折扣）两个维度——本模块用「档位 tiers」
覆盖分档；分时计价不在自动口径内（价格随请求时间变），需要时在配置价里自行建模。

不变量：**算不出来就不算**。未收录的模型返回 `priced=False`、费用 0，
绝不瞎猜一个数——本地 llama-server、自建网关、未收录新模型都走这条。
"""

import msgspec


class PriceTier(msgspec.Struct, frozen=True):
    """一个输入分档的单价（每百万 token；0 表示该流不计价）。"""

    up_to: int = 1 << 62
    """ 档位上限（含）：单次请求的 prompt_tokens <= up_to 落本档 """

    price_in: float = 0.0
    """ 普通输入单价 """

    price_out: float = 0.0
    """ 输出单价 """

    price_cached_in: float = 0.0
    """ 缓存命中（读）单价 """

    price_cache_write: float = 0.0
    """ 缓存写入单价（无缓存写计费的厂商填 0） """


class ModelPrice(msgspec.Struct, frozen=True):
    """一个模型的价格表：币种 + 按输入量升序的分档。"""

    currency: str = "USD"
    """ 币种（"USD" / "CNY" ...）——各家账单币种不同，分开累计不混算 """

    tiers: tuple[PriceTier, ...] = ()
    """ 分档（按 up_to 升序）；单档平价模型的 tiers 只有一档 """


class CostBreakdown(msgspec.Struct, frozen=True):
    """一次调用的费用明细（四条流 + 合计）。"""

    model: str = ""
    """ 实际计费依据的模型名（响应里的 model 优先，回落配置名） """

    currency: str = "USD"
    input_tokens: int = 0
    """ 普通输入 token（= prompt - cached） """

    cached_tokens: int = 0
    """ 缓存命中（读）token """

    cache_write_tokens: int = 0
    """ 缓存写入 token """

    output_tokens: int = 0
    input_cost: float = 0.0
    cached_cost: float = 0.0
    cache_write_cost: float = 0.0
    output_cost: float = 0.0
    total: float = 0.0
    priced: bool = False
    """ False = 无价可算（本地/未收录模型），此时 total=0，不是「免费」 """


def cost_of(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    price: ModelPrice | None,
    model: str = "",
) -> CostBreakdown:
    """纯计算：用量 x 价格 -> 费用明细。

    **计量口径（OpenAI 式）**：`prompt_tokens` 是输入**总量**，其中
    `cached_tokens`（缓存读）与 `cache_write_tokens`（缓存写）是它的子集，
    普通输入 = prompt - cached - cache_write。Provider 层负责把各家
    usage 归一到这个口径（Anthropic 把两者单列在 input 之外，不适用本引擎
    当前的 OpenAI 兼容实现，真接 Anthropic 协议时需在 Provider 侧换算）。

    Args:
        prompt_tokens: 输入总量（**含**缓存读与缓存写部分）
        completion_tokens: 输出总量（含思维链 token）
        cached_tokens: 其中命中缓存的部分（各家 usage 的 cached_tokens）
        cache_write_tokens: 其中写入缓存的部分（Anthropic/Kimi/阿里有；无则 0）
        price: 价格表；None = 未收录模型，返回 unpriced 明细
        model: 模型名（记账用）

    Returns:
        CostBreakdown: 明细；price 为 None 或空档位时 priced=False、各费用 0
    """
    fresh_in = max(0, prompt_tokens - cached_tokens - cache_write_tokens)
    base = CostBreakdown(
        model=model,
        currency=price.currency if price is not None else "USD",
        input_tokens=fresh_in,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        output_tokens=completion_tokens,
    )
    if price is None or not price.tiers:
        return base

    tier = _tier_for(prompt_tokens, price.tiers)
    input_cost = fresh_in * tier.price_in / 1_000_000
    cached_cost = cached_tokens * tier.price_cached_in / 1_000_000
    write_cost = cache_write_tokens * tier.price_cache_write / 1_000_000
    output_cost = completion_tokens * tier.price_out / 1_000_000
    return msgspec.structs.replace(
        base,
        input_tokens=fresh_in,
        input_cost=input_cost,
        cached_cost=cached_cost,
        cache_write_cost=write_cost,
        output_cost=output_cost,
        total=input_cost + cached_cost + write_cost + output_cost,
        priced=True,
    )


def _tier_for(prompt_tokens: int, tiers: tuple[PriceTier, ...]) -> PriceTier:
    """按输入量选档（调用方保证 tiers 已按 up_to 升序；超大输入落最后一档）。"""
    for tier in tiers:
        if prompt_tokens <= tier.up_to:
            return tier
    return tiers[-1]

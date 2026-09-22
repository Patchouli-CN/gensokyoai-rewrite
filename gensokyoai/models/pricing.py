"""内置模型价格表（2026-09 调研快照）+ 按模型名查找。

数据来源（2026-09-21/22 抓取，价格随时会变，**账对不上时以官网为准**）：
- OpenAI：<https://developers.openai.com/api/docs/pricing>（缓存读 = 输入 1 折）
- Anthropic：<https://platform.claude.com/docs/en/about-claude/pricing>（缓存读 1 折；
  缓存写列为 5min TTL 的 1.25×，1h TTL 为 2×——本表按 5min 记）
- DeepSeek：<https://api-docs.deepseek.com/quick_start/pricing>（**峰谷分时**：
  本表记忙时价，闲时半价；无缓存写计费）
- Google：<https://ai.google.dev/gemini-api/docs/pricing>（缓存读 1 折）
- 阿里百炼：<https://www.alibabacloud.com/help/en/model-studio/model-pricing>
  （qwen max/plus **按输入量分档**；缓存读 1 折、显式缓存写 1.25×）

未收录的模型（含本地 llama-server）返回 None，调用方按 unpriced 处理——
**算不出来就不算，绝不瞎猜**。要精确/要新模型，在 settings.yaml 的
`<模块>.price` 配置覆盖（优先级高于本表）。
"""

from ..schemas.cost_schema import ModelPrice, PriceTier
from ..utils.logger import LoggerManager

_logger = LoggerManager.get_logger("PRICING")

_USD = "USD"


def _flat(
    price_in: float, price_out: float, price_cached_in: float, price_cache_write: float = 0.0
) -> ModelPrice:
    """单档平价格式（大多数模型）。"""
    return ModelPrice(
        currency=_USD,
        tiers=(
            PriceTier(
                price_in=price_in,
                price_out=price_out,
                price_cached_in=price_cached_in,
                price_cache_write=price_cache_write,
            ),
        ),
    )


def _tiered(*rows: tuple[int, float, float, float, float]) -> ModelPrice:
    """分档价格（按输入量切价）：(up_to, in, out, cached_in, cache_write)。"""
    return ModelPrice(
        currency=_USD,
        tiers=tuple(
            PriceTier(
                up_to=r[0],
                price_in=r[1],
                price_out=r[2],
                price_cached_in=r[3],
                price_cache_write=r[4],
            )
            for r in rows
        ),
    )


# ---------------------------------------------------------------- 内置表
_BUILTIN_PRICES: dict[str, ModelPrice] = {
    # ---- OpenAI（缓存读 1 折；Batch 半价不录——本引擎不走 Batch API）----
    "gpt-5.6-sol": _flat(4.0, 20.0, 0.4),
    "gpt-5.6-terra": _flat(2.0, 12.0, 0.2),
    "gpt-5.6-luna": _flat(0.2, 1.2, 0.02),
    "gpt-5.5": _flat(5.0, 30.0, 0.5),
    "gpt-5.4": _flat(2.5, 15.0, 0.25),
    "gpt-5.4-mini": _flat(0.75, 4.5, 0.075),
    "gpt-5.4-nano": _flat(0.2, 1.25, 0.02),
    # ---- Anthropic（缓存读 1 折；写列 = 5min TTL 1.25×）----
    "claude-opus-5": _flat(5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": _flat(2.0, 10.0, 0.2, 2.5),
    "claude-haiku-4.5": _flat(1.0, 5.0, 0.1, 1.25),
    "claude-fable-5.1": _flat(10.0, 50.0, 0.25, 12.5),
    # ---- DeepSeek（忙时价；闲时全半价，无缓存写计费）----
    "deepseek-flash": _flat(0.3, 1.2, 0.006),
    "deepseek-v4-pro": _flat(1.32, 3.96, 0.044),
    # ---- Google Gemini ----
    "gemini-3.6-flash": _flat(1.5, 7.5, 0.15),
    "gemini-3.1-pro": _flat(2.0, 12.0, 0.2),
    "gemini-3.5-flash-lite": _flat(0.3, 2.5, 0.03),
    "gemini-2.5-flash": _flat(0.3, 2.5, 0.03),
    # ---- 阿里百炼 Qwen（国际站；缓存读 1 折、显式缓存写 1.25×）----
    "qwen3.8-max": _flat(2.0, 6.0, 0.2, 2.5),
    "qwen3-max": _tiered(
        (32_000, 1.2, 6.0, 0.12, 1.5),
        (128_000, 2.4, 12.0, 0.24, 3.0),
        (256_000, 3.0, 15.0, 0.3, 3.75),
    ),
    "qwen3.7-plus": _tiered(
        (256_000, 0.276, 1.101, 0.0276, 0.345), (1_000_000, 0.826, 3.301, 0.0826, 1.0325)
    ),
    "qwen-plus": _tiered(
        (128_000, 0.115, 0.287, 0.0115, 0.14375),
        (256_000, 0.345, 2.868, 0.0345, 0.43125),
        (1_000_000, 0.689, 6.881, 0.0689, 0.86125),
    ),
}

_warned_unknown: set[str] = set()
""" 已警告过的未收录模型名（防每一条消息都刷屏） """


def price_for(model: str) -> ModelPrice | None:
    """按模型名查内置价格；未收录返回 None（调用方按 unpriced 处理）。

    归一化：去空白 + 小写 + 剥掉 `提供商/` 前缀（兼容网关常见的
    `anthropic/claude-sonnet-5` 形态）。日期后缀（`-2026-05-20`）不剥——
    同名不同日期的模型价格可能不同，宁缺勿错。

    Args:
        model: 模型名（响应里的 model 或配置里的 model_name）

    Returns:
        ModelPrice | None: 内置价格；未收录 None
    """
    normalized = model.strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    price = _BUILTIN_PRICES.get(normalized)
    if price is None and normalized and normalized not in _warned_unknown:
        _warned_unknown.add(normalized)
        _logger.info(
            f"模型 {model!r} 无内置价格（本地/未收录），本次调用不计价；"
            "需要精确计量可在 settings.yaml 的 <模块>.price 配置价格"
        )
    return price

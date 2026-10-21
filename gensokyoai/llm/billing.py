"""计费：价格表 + 用量账本。

价格按"每百万 token 多少美元"记，前缀匹配模型名；没配价格就只记 token 不记钱。
"""

import msgspec

from .types import Usage

_PER_MILLION = 1_000_000.0


class Prices(msgspec.Struct, frozen=True):
    """一个模型的价格（USD / 百万 token）。"""

    input: float = 0.0
    output: float = 0.0
    cached: float = 0.0
    """ 命中缓存的输入价；0 表示按 input 算 """

    def cost(self, usage: Usage) -> float:
        """按用量算钱。"""
        cached = min(usage.cached_tokens, usage.prompt_tokens)
        fresh = max(0, usage.prompt_tokens - cached)
        cached_rate = self.cached or self.input
        return (
            fresh * self.input
            + cached * cached_rate
            + usage.completion_tokens * self.output
        ) / _PER_MILLION


BUILTIN: dict[str, Prices] = {
    "gpt-4o-mini": Prices(input=0.15, output=0.60),
    "gpt-4o": Prices(input=2.50, output=10.00),
    "deepseek-chat": Prices(input=0.27, output=1.10, cached=0.07),
    "deepseek-reasoner": Prices(input=0.55, output=2.19, cached=0.14),
    "claude-3-5-haiku": Prices(input=0.80, output=4.00),
    "claude-sonnet": Prices(input=3.00, output=15.00),
}


def price_for(model: str, table: dict[str, Prices] | None = None) -> Prices:
    """按模型名找价格（最长前缀优先，找不到算 0）。"""
    catalog = BUILTIN if table is None else table
    hits = [key for key in catalog if model and model.lower().startswith(key.lower())]
    if not hits:
        return Prices()
    return catalog[max(hits, key=len)]


class UsageLedger:
    """累计用量与花费（一次进程内共享）。"""

    def __init__(self, prices: Prices | None = None) -> None:
        self.prices = prices or Prices()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cost = 0.0
        self.calls = 0

    def record(self, usage: Usage) -> float:
        """记一次用量，返回这次花了多少。"""
        spent = self.prices.cost(usage)
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cached_tokens += usage.cached_tokens
        self.cost += spent
        return spent

    def summary(self) -> str:
        """一行摘要（给 /doctor 看）。"""
        return (
            f"{self.calls} 次调用 · 输入 {self.prompt_tokens} / 输出 "
            f"{self.completion_tokens} tokens · 约 ${self.cost:.4f}"
        )

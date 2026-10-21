"""计费与限额：按 token 算钱（按量付费），按滚动窗口看用了多少（订阅制的墙）。

两种口径可以同时开：本地模型不需要钱，但你可能想盯 7 天用了多少 token。
"""

import json
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import msgspec

from .types import Usage

_PER_MILLION = 1_000_000.0
_DEFAULT_SPAN_S = 7 * 24 * 3600.0
""" 没配额度时，账本最多回看这么久 """


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


class Quota(msgspec.Struct, frozen=True):
    """一个滚动窗口的额度（0 = 不限）。"""

    span_s: float
    tokens: int = 0
    cost: float = 0.0

    @property
    def label(self) -> str:
        """窗口名（给日志与 /doctor 看）。"""
        hours = self.span_s / 3600.0
        return f"{hours:g}h" if hours < 48 else f"{hours / 24:g}d"


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
    """累计用量、花费与滚动窗口额度。"""

    def __init__(
        self,
        prices: Prices | None = None,
        *,
        quotas: tuple[Quota, ...] = (),
        path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化。

        Args:
            prices: 单价（None = 不记钱）
            quotas: 滚动窗口额度
            path: 用量流水（JSONL；给了就落盘、重启接上）
            clock: 时间源（测试注入）
        """
        self.prices = prices or Prices()
        self.quotas = tuple(quotas)
        self._clock = clock
        self._path = Path(path) if path is not None else None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cost = 0.0
        self.calls = 0
        self._window: deque[tuple[float, int, float]] = deque()
        """ (时间, token 数, 花费) —— 只留最长窗口内的 """

        self._span = max(
            (quota.span_s for quota in self.quotas), default=_DEFAULT_SPAN_S
        )
        self._warned: set[tuple[float, str]] = set()
        if self._path is not None:
            self._load()

    def record(self, usage: Usage, model: str = "") -> float:
        """记一次用量，返回这次花了多少。"""
        spent = self.prices.cost(usage)
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cached_tokens += usage.cached_tokens
        self.cost += spent
        now = self._clock()
        self._window.append((now, usage.total_tokens, spent))
        self._trim(now)
        self._persist(now, usage, spent, model)
        self._check(now)
        return spent

    def window(self, span_s: float) -> tuple[int, float]:
        """某个窗口内用了多少：返回 (tokens, 花费)。"""
        self._trim(self._clock())
        since = self._clock() - span_s
        tokens = sum(item[1] for item in self._window if item[0] >= since)
        spent = sum(item[2] for item in self._window if item[0] >= since)
        return tokens, spent

    def status(self) -> list[str]:
        """每个窗口一行，给 /doctor 看。"""
        lines = []
        for quota in self.quotas:
            tokens, spent = self.window(quota.span_s)
            parts = [f"{tokens} tokens"]
            if quota.tokens:
                parts.append(f"/ {quota.tokens}（{tokens / quota.tokens:.0%}）")
            if quota.cost:
                parts.append(f"${spent:.4f} / ${quota.cost:.2f}")
            lines.append(f"{quota.label}: " + " ".join(parts) + " 剩余时间刷新")
        return lines

    def summary(self) -> str:
        """一行摘要。"""
        text = (
            f"{self.calls} 次调用 · 输入 {self.prompt_tokens} / 输出 "
            f"{self.completion_tokens} tokens · 约 ${self.cost:.4f}"
        )
        if self.quotas:
            text += " · " + "；".join(self.status())
        return text

    def _trim(self, now: float) -> None:
        """丢掉超出最长窗口的流水。"""
        while self._window and now - self._window[0][0] > self._span:
            self._window.popleft()

    def _check(self, now: float) -> None:
        """额度用量过线时告警（每个窗口每档只喊一次）。"""
        for quota in self.quotas:
            if not quota.tokens:
                continue
            used, _ = self.window(quota.span_s)
            ratio = used / quota.tokens
            for level, threshold in (("full", 1.0), ("high", 0.8)):
                if ratio >= threshold and (quota.span_s, level) not in self._warned:
                    self._warned.add((quota.span_s, level))
                    from ..utils.logger import LoggerManager

                    message = (
                        f"{quota.label} 窗口已用 {used}/{quota.tokens} tokens"
                        f"（{ratio:.0%}）"
                    )
                    LoggerManager.get_logger("BILLING").warning(
                        message + ("，额度用尽" if level == "full" else "，注意")
                    )
                    break

    def _persist(self, now: float, usage: Usage, spent: float, model: str) -> None:
        """记一行流水（不 flush：崩了最多丢最后几条）。"""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "ts": now,
                    "model": model,
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "cached": usage.cached_tokens,
                    "cost": round(spent, 8),
                },
                ensure_ascii=False,
            )
            with self._path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
        except OSError:
            from ..utils.logger import LoggerManager

            LoggerManager.get_logger("BILLING").warning(
                f"用量流水写不进去: {self._path}"
            )

    def _load(self) -> None:
        """读回窗口内的历史用量（坏的行走跳过）。"""
        if not self._path.exists():
            return
        now = self._clock()
        kept: list[tuple[float, int, float]] = []
        try:
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    stamp = float(item["ts"])
                    tokens = int(item["prompt"]) + int(item["completion"])
                    spent = float(item.get("cost") or 0.0)
                except (ValueError, TypeError, KeyError):
                    continue
                if now - stamp <= self._span:
                    kept.append((stamp, tokens, spent))
        except OSError:
            return
        kept.sort()
        self._window.extend(kept)

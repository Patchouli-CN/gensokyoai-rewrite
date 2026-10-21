"""计费口径 —— 一个 Protocol + 两种常见实现；各家的数字归各家适配器。

引擎只问三件事：`currency`（显示什么符号）、`cost()`（这一次花多少）、
`describe()`（给 /doctor 看的口径说明）。不做计费的厂商就不给 `billing`，
账本只记 token。
"""

import json
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import msgspec

from .types import Usage

PER_MILLION = 1_000_000.0
_DEFAULT_SPAN_S = 7 * 24 * 3600.0
""" 没配额度时，账本最多回看这么久 """

BEIJING = timezone(timedelta(hours=8))
""" 峰谷按北京时间算，不跟着机器时区走 """

_SYMBOLS = {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥"}


class Billing(Protocol):
    """一家厂商的计费口径。"""

    currency: str
    """ ISO 货币代码 """

    def cost(self, usage: Usage, when: float | None = None) -> float:
        """这一次调用花多少（缺口时刻就按现在）。"""
        ...

    def describe(self) -> str:
        """一行口径说明（给 /doctor 看）。"""
        ...


class Rates(msgspec.Struct, frozen=True):
    """每百万 token 的三个数字。"""

    input: float = 0.0
    output: float = 0.0
    cached: float = 0.0
    """ 命中缓存的输入价；0 表示按 input 算 """

    def cost(self, usage: Usage) -> float:
        """按这三档算钱。"""
        cached = min(usage.cached_tokens, usage.prompt_tokens)
        fresh = max(0, usage.prompt_tokens - cached)
        cached_rate = self.cached or self.input
        return (
            fresh * self.input
            + cached * cached_rate
            + usage.completion_tokens * self.output
        ) / PER_MILLION


class FlatBilling:
    """不分时段的一口价。"""

    def __init__(self, rates: Rates, *, currency: str = "USD", label: str = "") -> None:
        self.rates = rates
        self.currency = currency
        self.label = label or currency

    def cost(self, usage: Usage, when: float | None = None) -> float:
        """按用量算钱（时段无关）。"""
        return self.rates.cost(usage)

    def describe(self) -> str:
        """一行口径。"""
        rates = self.rates
        cached = f"，缓存命中 {rates.cached:g}" if rates.cached else ""
        return (
            f"{symbol(self.currency)}{rates.input:g} 输入 / "
            f"{rates.output:g} 输出（每百万 token{cached}）"
        )


class PeakBilling:
    """峰谷两档：平常价 + 高峰价 + 时段表（北京时间，节假日除外）。"""

    def __init__(
        self,
        off: Rates,
        peak: Rates,
        *,
        currency: str = "CNY",
        weekdays: tuple[int, ...] = (1, 2, 3, 4, 5),
        hours: tuple[tuple[int, int], ...] = ((9, 12), (14, 18)),
        holidays: tuple[str, ...] = (),
        label: str = "",
    ) -> None:
        """初始化。

        Args:
            off: 空闲时段价
            peak: 高峰时段价
            currency: ISO 货币代码
            weekdays: 高峰是哪几天（ISO：1 = 周一）
            hours: 高峰时段（左闭右开，北京时间的小时）
            holidays: 法定节假日（"2026-01-01"），这些天全天算空闲
            label: 显示名
        """
        self.off = off
        self.peak = peak
        self.currency = currency
        self.weekdays = tuple(weekdays)
        self.hours = tuple(hours)
        self.holidays = tuple(holidays)
        self.label = label or currency

    def is_peak(self, when: float) -> bool:
        """某个时刻算不算高峰。"""
        moment = datetime.fromtimestamp(when, BEIJING)
        if moment.strftime("%Y-%m-%d") in self.holidays:
            return False
        if moment.isoweekday() not in self.weekdays:
            return False
        return any(start <= moment.hour < end for start, end in self.hours)

    def cost(self, usage: Usage, when: float | None = None) -> float:
        """按用量算钱（按调用时刻挑档）。"""
        rates = (
            self.peak
            if self.is_peak(time.time() if when is None else when)
            else self.off
        )
        return rates.cost(usage)

    def describe(self) -> str:
        """一行口径。"""
        spans = "、".join(f"{start}-{end}" for start, end in self.hours)
        return (
            f"{symbol(self.currency)}{self.off.input:g}/{self.off.output:g} 空闲，"
            f"{self.peak.input:g}/{self.peak.output:g} 高峰"
            f"（工作日 {spans} 北京时间；节假日全日空闲）"
        )


def symbol(currency: str) -> str:
    """货币符号。"""
    return _SYMBOLS.get(currency.upper(), currency + " ")


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


class UsageLedger:
    """累计用量、花费与滚动窗口额度。"""

    def __init__(
        self,
        billing: Billing | None = None,
        *,
        quotas: tuple[Quota, ...] = (),
        path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化。

        Args:
            billing: 计费口径（None = 厂商不提供，只记 token）
            quotas: 滚动窗口额度
            path: 用量流水（JSONL；给了就落盘、重启接上）
            clock: 时间源（测试注入）
        """
        self.billing = billing
        self.quotas = tuple(quotas)
        self._clock = clock
        self._path = Path(path) if path is not None else None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.reasoning_tokens = 0
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

    @property
    def currency(self) -> str:
        """计价货币（没有计费口径就是空串）。"""
        return self.billing.currency if self.billing is not None else ""

    def record(self, usage: Usage, model: str = "") -> float:
        """记一次用量，返回这次花了多少。"""
        now = self._clock()
        spent = self.billing.cost(usage, now) if self.billing is not None else 0.0
        self.calls += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.cached_tokens += usage.cached_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.cost += spent
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
        mark = symbol(self.currency) if self.currency else ""
        lines = []
        for quota in self.quotas:
            tokens, spent = self.window(quota.span_s)
            parts = [f"{tokens} tokens"]
            if quota.tokens:
                parts.append(f"/ {quota.tokens}（{tokens / quota.tokens:.0%}）")
            if quota.cost:
                parts.append(f"{mark}{spent:.4f} / {mark}{quota.cost:.2f}")
            lines.append(f"{quota.label}: " + " ".join(parts))
        return lines

    def summary(self) -> str:
        """一行摘要。"""
        text = (
            f"{self.calls} 次调用 · 输入 {self.prompt_tokens} / 输出 "
            f"{self.completion_tokens} tokens"
            + (f"（思考 {self.reasoning_tokens}）" if self.reasoning_tokens else "")
        )
        if self.billing is not None:
            text += f" · 约 {symbol(self.currency)}{self.cost:.4f}"
        else:
            text += " · 该厂商未提供计费"
        if self.quotas:
            text += " · " + "；".join(self.status())
        return text

    def describe(self) -> str:
        """计费口径说明（/doctor 用）。"""
        if self.billing is None:
            return "未提供计费口径（只记 token）"
        return self.billing.describe()

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
                    "currency": self.currency,
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

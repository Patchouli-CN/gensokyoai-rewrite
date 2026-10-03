"""供应商限流窗口快照 —— 滚动窗口额度（5h/7d 这类订阅配额）的采集与展示。

数据流：

    Provider 每次调用顺手把响应头喂进来（models/base.py 的 POST 处）
        -> RateLimitRegistry 按 base_url 存最新快照
        -> /quota 指令读快照展示「剩余 X%（重置时间）」

支持两族响应头：

- **Anthropic**: `anthropic-ratelimit-{requests,tokens}-{limit,remaining,reset}`
  （reset 为 RFC3339 时间戳）
- **OpenAI 系**: `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`
  （reset 为时长字符串，如 "6m0s"）

供应商不带这些头就静默跳过——本模块是纯观测管道，不参与任何决策。
"""

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

import msgspec

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
""" OpenAI reset 时长串的单个分量，如 "6m0s" 里的 6m 与 0s """

_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class RateLimitWindow(msgspec.Struct, frozen=True):
    """一个限流窗口的最新状态（requests 或 tokens 维）"""

    limit: int = 0
    """ 窗口总额度（0 = 供应商没给） """
    remaining: int = 0
    """ 剩余额度 """
    reset_at: float | None = None
    """ 重置时刻（epoch 秒；None = 供应商没给） """

    @property
    def remaining_ratio(self) -> float | None:
        """剩余比例（0~1）；无限额信息时为 None"""
        if self.limit <= 0:
            return None
        return min(1.0, max(0.0, self.remaining / self.limit))


@dataclass(slots=True)
class ProviderRateLimits:
    """单个供应商（base_url）的最新窗口快照"""

    windows: dict[str, RateLimitWindow] = field(default_factory=dict)
    """ 维度名（"requests"/"tokens"）-> 窗口状态 """
    updated_at: float = 0.0
    """ 快照采集时刻（epoch 秒） """


def _parse_duration(text: str, now: float) -> float | None:
    """OpenAI 时长串（"6m0s" / "500ms"）-> 重置时刻（epoch 秒）；解析失败 None"""
    total = 0.0
    matched = False
    for value, unit in _DURATION_PART.findall(text):
        total += float(value) * _UNIT_SECONDS[unit]
        matched = True
    return now + total if matched else None


def _parse_timestamp(text: str) -> float | None:
    """RFC3339 时间戳 -> epoch 秒；解析失败 None"""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _int_or_none(headers: Mapping[str, str], key: str) -> int | None:
    """从响应头取整数值；缺失/非数字返回 None"""
    raw = headers.get(key)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def parse_rate_limit_headers(
    headers: Mapping[str, str], *, now: float | None = None
) -> dict[str, RateLimitWindow]:
    """从一次响应头解析限流窗口（两族头部自适应；无相关头返回空 dict）。

    Args:
        headers: 响应头（aiohttp 的 CIMultiDict 大小写不敏感，普通 dict 也可）
        now: 当前时刻（OpenAI 时长串要折算成绝对时刻；可注入以便测试）

    Returns:
        dict: 维度名 -> 窗口状态（只含实际解析到的维度）
    """
    if now is None:
        now = time.time()
    windows: dict[str, RateLimitWindow] = {}

    # Anthropic 族：{requests,tokens}-{limit,remaining,reset}（reset 为时间戳）
    for dim in ("requests", "tokens"):
        limit = _int_or_none(headers, f"anthropic-ratelimit-{dim}-limit")
        remaining = _int_or_none(headers, f"anthropic-ratelimit-{dim}-remaining")
        reset_raw = headers.get(f"anthropic-ratelimit-{dim}-reset")
        if limit is None and remaining is None:
            continue
        windows[dim] = RateLimitWindow(
            limit=limit or 0,
            remaining=remaining or 0,
            reset_at=_parse_timestamp(reset_raw) if reset_raw else None,
        )

    # OpenAI 族：{limit,remaining,reset}-{requests,tokens}（reset 为时长串）
    for dim in ("requests", "tokens"):
        limit = _int_or_none(headers, f"x-ratelimit-limit-{dim}")
        remaining = _int_or_none(headers, f"x-ratelimit-remaining-{dim}")
        reset_raw = headers.get(f"x-ratelimit-reset-{dim}")
        if limit is None and remaining is None:
            continue
        windows[dim] = RateLimitWindow(
            limit=limit or 0,
            remaining=remaining or 0,
            reset_at=_parse_duration(reset_raw, now) if reset_raw else None,
        )

    return windows


class RateLimitRegistry:
    """进程级限流快照注册表：Provider 自报，指令层只读。

    引擎是单进程，这里用模块级单例（与 LoggerManager 同款思路）；
    测试用 `reset()` 隔离。
    """

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._providers: dict[str, ProviderRateLimits] = {}

    def note(self, base_url: str, headers: Mapping[str, str]) -> None:
        """采集一次响应头；解析不到限流信息就什么都不记（本地/不支持的头）。"""
        windows = parse_rate_limit_headers(headers, now=self._clock())
        if not windows:
            return
        entry = self._providers.setdefault(base_url, ProviderRateLimits())
        entry.windows.update(windows)
        entry.updated_at = self._clock()

    def snapshot(self) -> dict[str, ProviderRateLimits]:
        """全部供应商的最新快照（键为 base_url；无任何记录返回空 dict）。"""
        return {
            url: ProviderRateLimits(windows=dict(entry.windows), updated_at=entry.updated_at)
            for url, entry in self._providers.items()
        }

    def reset(self) -> None:
        """清空（测试隔离用）。"""
        self._providers.clear()


RATE_LIMITS = RateLimitRegistry()
""" 进程级单例：models 层写入，指令层读取 """

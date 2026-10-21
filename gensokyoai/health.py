"""健康中心 —— 各器官直报指标，集中出报告。

没有 event bus：调用即记账。三种指标：
- **gauge**：注册一个读数器，出报告时现读（房间数、记忆条数、在线连接）
- **counter**：只增计数（回合数、被注意力压掉的次数、工具调用数）
- **sample**：数值采样，出报告给 avg/p95（回合延迟）
"""

import time
from collections import deque
from collections.abc import Callable

_MAX_SAMPLES = 200
""" 每类采样保留的最近点数（出 p95 用，超了滚动丢弃） """


class HealthCenter:
    """运行指标集散地（一个进程一个，hub 持有并传各器官）。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._gauges: dict[str, Callable[[], float]] = {}
        self._counters: dict[str, float] = {}
        self._samples: dict[str, deque[float]] = {}

    def gauge(self, name: str, reader: Callable[[], float]) -> None:
        """注册读数器（出报告时现读）。"""
        self._gauges[name] = reader

    def count(self, name: str, amount: float = 1.0) -> None:
        """计数累加。"""
        self._counters[name] = self._counters.get(name, 0.0) + amount

    def observe(self, name: str, value: float) -> None:
        """采样一个数值（如回合延迟秒数）。"""
        samples = self._samples.setdefault(name, deque(maxlen=_MAX_SAMPLES))
        samples.append(value)

    @property
    def uptime(self) -> float:
        """已经跑了多少秒。"""
        return self._clock() - self._started

    def counter(self, name: str) -> float:
        """取一个计数值（没有就是 0）。"""
        return self._counters.get(name, 0.0)

    def counters(self) -> dict[str, float]:
        """全部计数（只读副本）。"""
        return dict(self._counters)

    def latency(self, name: str) -> tuple[float, float, int] | None:
        """某个采样的 (平均, p95, 次数)；没有采样返回 None。"""
        samples = self._samples.get(name)
        if not samples:
            return None
        ordered = sorted(samples)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return sum(ordered) / len(ordered), p95, len(ordered)

    def gauge_value(self, name: str) -> float | None:
        """读一次 gauge（没有就是 None）。"""
        reader = self._gauges.get(name)
        return None if reader is None else reader()

    def report(self) -> str:
        """渲染原始指标（内部名 + 数字，排查用）。"""
        lines = [f"运行 {self.uptime / 60:.1f} 分钟"]
        if self._counters:
            lines.append(
                "计数: "
                + ", ".join(f"{k}={v:g}" for k, v in sorted(self._counters.items()))
            )
        for name in sorted(self._samples):
            stats = self.latency(name)
            if stats is not None:
                avg, p95, count = stats
                lines.append(f"{name}: avg={avg:.2f} p95={p95:.2f} (n={count})")
        if self._gauges:
            lines.append(
                "读数: "
                + ", ".join(
                    f"{k}={reader():g}" for k, reader in sorted(self._gauges.items())
                )
            )
        return "\n".join(lines)

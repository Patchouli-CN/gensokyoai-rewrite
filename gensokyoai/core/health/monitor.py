"""系统健康监控 —— HealthCenter（架构文档 §3.5）

职责是「监控 **+ 主动干预**」，旧实现只有前半截：阈值判断一律 `value >= threshold`、
会话摘要是写死的 stub、而且发了告警事件**没人订阅**。

现在补齐四件事：

- **阈值带方向**：`MetricThreshold.lower_is_worse` 区分「越高越坏 / 越低越坏」
- **边沿触发 + 冷却**：指标持续超限不会每回合刷告警（原先会刷屏、干预会反复触发）
- **聚合摘要**：报告给 avg/min/max/p95，而不是把上百条原始样本全 dump 出去
- **主动干预**：`register_intervention(metric, handler)` 注册回调，超限时被调用。
  回调由**装配层**（TouhouWorld）注册，因此本模块不反向依赖 memorizer / roleplay，
  分层铁律不破。
"""

import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from ...schemas.event_schema import BaseEvent, EventTopic
from ...schemas.health_schema import (
    HealthAlert,
    HealthMetric,
    HealthReport,
    MetricSummary,
    MetricThreshold,
)
from ...utils.logger import LoggerManager
from ..event_bus import EventBus

InterventionHandler = Callable[[HealthAlert], Awaitable[None]]
""" 主动干预回调：收到告警后做点什么（压缩记忆 / 调高档位 / 归档冷记忆…）"""

SummaryProvider = Callable[[], Any]
""" 摘要提供者：由装配层注入（避免 core/health 反向依赖 SessionManager/ResourceGate）"""

DEFAULT_THRESHOLDS: tuple[MetricThreshold, ...] = (
    MetricThreshold(name="turn.latency_s", threshold=30.0, unit="s"),
    MetricThreshold(name="memory.work_size", threshold=500.0, unit="条"),
    MetricThreshold(name="memory.long_size", threshold=5000.0, unit="条"),
    MetricThreshold(name="ooc.rate", threshold=0.5, unit="ratio"),
    MetricThreshold(name="session.context_usage", threshold=0.8, unit="ratio"),
    MetricThreshold(name="session.count", threshold=10.0, unit="个"),
)
""" 默认阈值表 """

DEFAULT_ALERT_COOLDOWN = 60.0
""" 同级别告警的冷却秒数（防刷屏 / 防干预反复触发）"""


class HealthMonitor:
    """系统健康状态监控 + 主动干预。

    Attributes:
        thresholds: 生效的阈值表（按 name 索引）
    """

    def __init__(
        self,
        bus: EventBus | None = None,
        *,
        thresholds: tuple[MetricThreshold, ...] | list[MetricThreshold] | None = None,
        session_provider: SummaryProvider | None = None,
        quota_provider: SummaryProvider | None = None,
        history_size: int = 100,
        alert_cooldown: float = DEFAULT_ALERT_COOLDOWN,
        clock=time.time,
    ) -> None:
        """初始化。

        Args:
            bus: 事件总线；非空时告警会以 HEALTH_ALERT 广播
            thresholds: 阈值表；None 用 DEFAULT_THRESHOLDS
            session_provider: 返回会话摘要的回调（装配层注入）
            quota_provider: 返回配额摘要的回调（装配层注入）
            history_size: 每个指标保留的样本数
            alert_cooldown: 同级别告警冷却秒数
            clock: 时钟函数（可注入以便测试）
        """
        self._logger = LoggerManager.get_logger("HEALTH CENTER")
        self._bus = bus
        self._clock = clock
        self._history_size = history_size
        self._alert_cooldown = alert_cooldown
        self._session_provider = session_provider
        self._quota_provider = quota_provider

        table = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
        self.thresholds: dict[str, MetricThreshold] = {t.name: t for t in table}

        self._metrics: dict[str, deque[HealthMetric]] = {}
        self._alerts: list[HealthAlert] = []
        self._interventions: dict[str, list[InterventionHandler]] = {}
        self._alert_state: dict[str, tuple[str, float]] = {}
        """ 指标名 -> (上次告警级别, 时刻)，用于边沿触发与冷却 """
        self._started_at = self._clock()

    # ---------------------------------------------------------------- 记录

    async def record(self, data: dict) -> None:
        """记录一条指标。

        Args:
            data: 必须含 "name" 与 "value"，可选 "unit" / "tags"
        """
        name = data.get("name", "")
        if not name:
            self._logger.warning(f"指标缺少 name: {data}")
            return
        metric = HealthMetric(
            name=name,
            value=float(data.get("value", 0.0)),
            unit=str(data.get("unit", "")),
            timestamp=self._clock(),
            tags=dict(data.get("tags", {})),
        )
        self._metrics.setdefault(name, deque(maxlen=self._history_size)).append(metric)
        await self._check_alerts(metric)

    async def record_metric(
        self, name: str, value: float, unit: str = "", tags: dict[str, str] | None = None
    ) -> None:
        """便捷接口：记录一条指标。

        Args:
            name: 指标名
            value: 指标值
            unit: 单位
            tags: 附加标签
        """
        await self.record({"name": name, "value": value, "unit": unit, "tags": tags or {}})

    # ---------------------------------------------------------------- 干预

    def register_intervention(self, metric_name: str, handler: InterventionHandler) -> None:
        """为某指标注册主动干预回调（超限时被 await 调用）。

        Args:
            metric_name: 指标名
            handler: 干预回调，接收该次告警
        """
        self._interventions.setdefault(metric_name, []).append(handler)
        self._logger.info(f"注册健康干预: {metric_name} -> {getattr(handler, '__name__', handler)}")

    # ---------------------------------------------------------------- 查询

    def summary(self, name: str) -> MetricSummary:
        """取某指标的聚合摘要。

        Args:
            name: 指标名

        Returns:
            MetricSummary: 样本数 / 最近值 / 均值 / 极值 / p95
        """
        history = self._metrics.get(name)
        if not history:
            return MetricSummary(name=name)
        values = [m.value for m in history]
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return MetricSummary(
            name=name,
            count=len(values),
            last=values[-1],
            avg=sum(values) / len(values),
            minimum=ordered[0],
            maximum=ordered[-1],
            p95=ordered[p95_index],
        )

    async def get_metric_history(self, name: str, limit: int = 10) -> list[HealthMetric]:
        """取某指标的最近若干条原始样本。

        Args:
            name: 指标名
            limit: 条数上限

        Returns:
            list[HealthMetric]: 最近的样本（旧 -> 新）
        """
        return list(self._metrics.get(name, deque()))[-limit:]

    def alerts(self, limit: int = 10) -> list[HealthAlert]:
        """取最近的告警。

        Args:
            limit: 条数上限

        Returns:
            list[HealthAlert]: 最近告警
        """
        return self._alerts[-limit:]

    async def report(self) -> HealthReport:
        """生成健康报告（聚合摘要 + 推理档位分布 + 系统摘要）。

        Returns:
            HealthReport: 健康报告
        """
        now = self._clock()
        detail: dict[str, Any] = {
            "uptime_seconds": now - self._started_at,
            "summaries": {name: self.summary(name) for name in self._metrics},
            "reasoning_distribution": self.reasoning_distribution(),
            "alerts": self.alerts(),
            "system": {
                "sessions": self._session_provider() if self._session_provider else {},
                "quota": self._quota_provider() if self._quota_provider else [],
            },
        }
        report = HealthReport(functional="health", report_detail=detail, timestamp=now)
        self._logger.debug(
            f"健康报告生成: {len(detail['summaries'])} 指标, {len(self._alerts)} 告警"
        )
        return report

    def reasoning_distribution(self) -> dict[str, float]:
        """推理档位分布（由 `effort.<档位>` 计数聚合而来）。

        Returns:
            dict[str, float]: 档位 -> 累计回合数
        """
        dist: dict[str, float] = {}
        for name, history in self._metrics.items():
            if not name.startswith("effort."):
                continue
            dist[name[len("effort.") :]] = sum(m.value for m in history)
        return dist

    # ---------------------------------------------------------------- 内部

    async def _check_alerts(self, metric: HealthMetric) -> None:
        """按方向判阈值；边沿触发 + 冷却后告警并跑干预。"""
        threshold = self.thresholds.get(metric.name)
        if threshold is None:
            return

        breached, level = self._evaluate(metric.value, threshold)
        if not breached:
            # 指标恢复健康 -> 清掉告警状态，下次超限能重新告警
            self._alert_state.pop(metric.name, None)
            return

        now = self._clock()
        previous = self._alert_state.get(metric.name)
        if previous is not None:
            last_level, last_at = previous
            if last_level == level and now - last_at < self._alert_cooldown:
                return  # 同级别且冷却中：不重复告警

        alert = HealthAlert(
            level=level,
            source="health",
            message=(
                f"指标 {metric.name}={metric.value:.3f}{metric.unit} "
                f"{'低于' if threshold.lower_is_worse else '超过'}阈值 "
                f"{threshold.threshold:.3f}{threshold.unit or metric.unit}"
            ),
            metric=metric,
            timestamp=now,
        )
        self._alert_state[metric.name] = (level, now)
        self._alerts.append(alert)
        self._logger.warning(f"健康告警: {alert.message}")

        if self._bus is not None:
            await self._bus.publish(
                BaseEvent(
                    topic=EventTopic.HEALTH_ALERT,
                    source="health",
                    payload=alert,
                    timestamp=now,
                )
            )
        await self._run_interventions(alert)

    @staticmethod
    def _evaluate(value: float, threshold: MetricThreshold) -> tuple[bool, str]:
        """按阈值方向判断是否告警。

        Args:
            value: 指标值
            threshold: 阈值定义

        Returns:
            tuple[bool, str]: (是否超限, 告警级别)
        """
        if threshold.lower_is_worse:
            return (value <= threshold.threshold, "WARNING")
        if value < threshold.threshold:
            return (False, "")
        level = (
            "CRITICAL" if value >= threshold.threshold * threshold.critical_factor else "WARNING"
        )
        return (True, level)

    async def _run_interventions(self, alert: HealthAlert) -> None:
        """执行该指标注册的干预回调（异常隔离，不影响监控主链路）。"""
        if alert.metric is None:
            return
        for handler in self._interventions.get(alert.metric.name, []):
            try:
                await handler(alert)
            except Exception:
                self._logger.exception(f"健康干预执行失败: {alert.metric.name}")

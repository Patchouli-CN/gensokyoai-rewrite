"""系统健康监控 —— HealthCenter"""

import time
from collections import deque
from typing import Any

from ...schemas.event_schema import BaseEvent, EventTopic
from ...schemas.health_schema import HealthAlert, HealthMetric, HealthReport
from ...utils.logger import LoggerManager
from ..event_bus import EventBus


class HealthMonitor:
    """系统健康状态监控。

    职责：
    - 记录各模块上报的指标
    - 监控关键指标（推理延迟、记忆库大小、会话数量等）
    - 异常时通过 EventBus 发布 HEALTH_ALERT 事件
    - 生成健康报告
    """

    # 指标历史窗口大小
    _HISTORY_SIZE = 100

    # 告警阈值
    _ALERT_THRESHOLDS = {
        "inference_latency_ms": 30000,  # 30 秒
        "memory_size": 5000,  # 5000 条记忆
        "session_count": 10,  # 10 个会话
        "ooc_rate": 0.5,  # 50% 出戏率
    }

    def __init__(self, bus: EventBus | None = None) -> None:
        self._logger = LoggerManager.get_logger("HEALTH CENTER")
        self._bus = bus
        self._metrics: dict[str, deque[HealthMetric]] = {}
        self._alerts: list[HealthAlert] = []
        self._started_at = time.time()
        self._last_report_at = 0.0

    async def record(self, data: dict) -> None:
        """记录一条指标。

        Args:
            data: 指标数据字典，必须包含 "name" 和 "value"，可选 "unit" / "tags"
        """
        name = data.get("name", "")
        value = data.get("value", 0.0)
        if not name:
            self._logger.warning(f"指标缺少 name: {data}")
            return

        metric = HealthMetric(
            name=name,
            value=float(value),
            unit=str(data.get("unit", "")),
            timestamp=time.time(),
            tags=dict(data.get("tags", {})),
        )

        self._metrics.setdefault(name, deque(maxlen=self._HISTORY_SIZE)).append(metric)

        # 检查阈值，触发告警
        await self._check_alerts(metric)

    async def report(self) -> HealthReport:
        """生成健康报告。"""
        now = time.time()
        self._last_report_at = now

        report_detail: dict[str, Any] = {
            "uptime_seconds": now - self._started_at,
            "metrics": {name: list(history) for name, history in self._metrics.items()},
            "alerts": list(self._alerts[-10:]),  # 最近 10 条告警
            "system": {
                "memory": self._get_memory_summary(),
                "sessions": self._get_session_summary(),
            },
        }

        report = HealthReport(
            functional="health",
            report_detail=report_detail,
            timestamp=now,
        )

        self._logger.debug(
            f"健康报告生成: {len(report_detail['metrics'])} 指标, {len(self._alerts)} 告警"
        )
        return report

    # --- 辅助方法 ---

    async def _check_alerts(self, metric: HealthMetric) -> None:
        """根据阈值检查是否需要告警"""
        threshold = self._ALERT_THRESHOLDS.get(metric.name)
        if threshold is None:
            return

        if metric.value >= threshold:
            level = "WARNING"
            if metric.value >= threshold * 2:
                level = "CRITICAL"

            alert = HealthAlert(
                level=level,
                source="health",
                message=f"指标 {metric.name}={metric.value:.2f}{metric.unit} 超过阈值 {threshold:.2f}{metric.unit}",
                metric=metric,
                timestamp=time.time(),
            )
            self._alerts.append(alert)
            self._logger.warning(f"健康告警: {alert.message}")

            # 通过 EventBus 发布告警
            if self._bus:
                await self._bus.publish(
                    BaseEvent(
                        topic=EventTopic.HEALTH_ALERT,
                        source="health",
                        payload=alert,
                        timestamp=alert.timestamp,
                    )
                )

    def _get_memory_summary(self) -> dict[str, Any]:
        """内存使用摘要"""
        import psutil

        try:
            mem = psutil.virtual_memory()
            return {
                "total_gb": round(mem.total / (1024**3), 2),
                "available_gb": round(mem.available / (1024**3), 2),
                "used_percent": mem.percent,
            }
        except Exception:
            return {"error": "psutil not available"}

    def _get_session_summary(self) -> dict[str, Any]:
        """会话摘要（从 SessionManager 获取）"""
        # 这里需要注入 SessionManager，或者由调用方传入
        # 暂时返回空，后续可以改进
        return {"total": 0, "active": 0}

    # --- 供外部调用的接口 ---

    async def record_metric(
        self, name: str, value: float, unit: str = "", tags: dict[str, str] | None = None
    ) -> None:
        """便捷接口：记录一条指标"""
        await self.record({"name": name, "value": value, "unit": unit, "tags": tags or {}})

    async def get_metric_history(self, name: str, limit: int = 10) -> list[HealthMetric]:
        """获取某指标的历史数据"""
        history = self._metrics.get(name, deque())
        return list(history)[-limit:]

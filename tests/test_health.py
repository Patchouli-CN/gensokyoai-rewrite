"""健康监控单元测试：阈值方向 / 边沿触发与冷却 / 聚合 / 主动干预 / 报告"""

from gensokyoai.core.event_bus import EventBus
from gensokyoai.core.health import HealthMonitor
from gensokyoai.schemas.event_schema import EventTopic
from gensokyoai.schemas.health_schema import MetricThreshold


class _Clock:
    """可控时钟（秒）。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _monitor(**kwargs) -> HealthMonitor:
    kwargs.setdefault("clock", _Clock())
    return HealthMonitor(**kwargs)


async def test_higher_is_worse_alert_levels():
    """「越高越坏」：达阈值 WARNING，超倍数升 CRITICAL"""
    clock = _Clock()
    monitor = HealthMonitor(
        thresholds=[MetricThreshold(name="latency", threshold=10.0, critical_factor=2.0)],
        clock=clock,
    )
    await monitor.record_metric("latency", 5.0)
    assert monitor.alerts() == []

    await monitor.record_metric("latency", 10.0)
    assert monitor.alerts()[-1].level == "WARNING"

    clock.advance(100)  # 出冷却
    await monitor.record_metric("latency", 25.0)
    assert monitor.alerts()[-1].level == "CRITICAL"


async def test_lower_is_worse_direction():
    """「越低越坏」：高于阈值算健康，低于才告警（旧实现把方向判反）"""
    monitor = _monitor(
        thresholds=[MetricThreshold(name="free_quota", threshold=10.0, lower_is_worse=True)]
    )
    await monitor.record_metric("free_quota", 50.0)
    assert monitor.alerts() == [], "高于阈值应视为健康"

    await monitor.record_metric("free_quota", 5.0)
    assert monitor.alerts()[-1].level == "WARNING"


async def test_alert_cooldown_suppresses_repeats():
    """冷却期内同级别告警不重复（原实现每回合刷屏）"""
    clock = _Clock()
    monitor = HealthMonitor(
        thresholds=[MetricThreshold(name="latency", threshold=10.0)],
        alert_cooldown=60.0,
        clock=clock,
    )
    await monitor.record_metric("latency", 20.0)
    await monitor.record_metric("latency", 21.0)
    assert len(monitor.alerts()) == 1

    clock.advance(61)
    await monitor.record_metric("latency", 22.0)
    assert len(monitor.alerts()) == 2


async def test_recovery_allows_new_alert():
    """指标恢复健康后清状态，再次超限能重新告警"""
    clock = _Clock()
    monitor = HealthMonitor(
        thresholds=[MetricThreshold(name="latency", threshold=10.0)], clock=clock
    )
    await monitor.record_metric("latency", 20.0)
    await monitor.record_metric("latency", 1.0)  # 恢复
    await monitor.record_metric("latency", 20.0)  # 立即再超限
    assert len(monitor.alerts()) == 2


async def test_intervention_invoked_on_breach():
    """超限触发注册的主动干预回调"""
    monitor = _monitor(thresholds=[MetricThreshold(name="session.context_usage", threshold=0.8)])
    seen: list[float] = []

    async def handler(alert) -> None:
        seen.append(alert.metric.value)

    monitor.register_intervention("session.context_usage", handler)
    await monitor.record_metric("session.context_usage", 0.9)
    assert seen == [0.9]


async def test_intervention_exception_isolated():
    """干预回调抛异常不影响监控主链路"""
    monitor = _monitor(thresholds=[MetricThreshold(name="x", threshold=1.0)])

    async def bad(alert) -> None:
        raise RuntimeError("boom")

    monitor.register_intervention("x", bad)
    await monitor.record_metric("x", 5.0)
    assert monitor.alerts(), "告警照常记录"


async def test_no_threshold_means_no_alert():
    """未配置阈值的指标只记录、不告警"""
    monitor = _monitor(thresholds=[])
    await monitor.record_metric("whatever", 1e9)
    assert monitor.alerts() == []


async def test_summary_aggregates():
    """聚合摘要：样本数 / 最近值 / 极值 / 均值 / p95"""
    monitor = _monitor()
    for value in [1.0, 2.0, 3.0, 4.0, 100.0]:
        await monitor.record_metric("t", value)

    summary = monitor.summary("t")
    assert summary.count == 5
    assert summary.last == 100.0
    assert summary.minimum == 1.0
    assert summary.maximum == 100.0
    assert summary.avg == 22.0
    assert summary.p95 == 100.0


async def test_summary_of_unknown_metric_is_empty():
    """未知指标的摘要为零值"""
    assert _monitor().summary("nope").count == 0


async def test_report_includes_distribution_and_providers():
    """报告含档位分布与注入的系统摘要（不再 dump 原始序列）"""
    monitor = _monitor(
        session_provider=lambda: {"total": 2.0, "context_usage": 0.3},
        quota_provider=lambda: [{"tenant": "a"}],
    )
    await monitor.record({"name": "effort.LOW", "value": 1})
    await monitor.record({"name": "effort.HIGH", "value": 1})
    await monitor.record_metric("turn.latency_s", 1.5, unit="s")

    report = await monitor.report()
    detail = report.report_detail
    assert detail["reasoning_distribution"] == {"LOW": 1.0, "HIGH": 1.0}
    assert detail["system"]["sessions"] == {"total": 2.0, "context_usage": 0.3}
    assert detail["system"]["quota"] == [{"tenant": "a"}]
    assert detail["summaries"]["turn.latency_s"].count == 1
    assert "uptime_seconds" in detail


async def test_alert_published_on_bus():
    """告警通过事件总线广播（供外部订阅）"""
    bus = EventBus()
    seen: list = []

    async def on_alert(event) -> None:
        seen.append(event)

    bus.subscribe(EventTopic.HEALTH_ALERT, on_alert)
    monitor = HealthMonitor(
        bus=bus, thresholds=[MetricThreshold(name="x", threshold=1.0)], clock=_Clock()
    )
    await monitor.record_metric("x", 5.0)
    assert len(seen) == 1

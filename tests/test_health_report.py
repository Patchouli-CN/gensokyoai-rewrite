"""HealthReporter 组件测试 —— 回合指标真的逐条落进 HealthMonitor（含告警触发）。

只需要 SessionManager + 鸭子类型 memory + 真 HealthMonitor，不组装世界。
"""

import time
import types

from gensokyoai.core.health import HealthMonitor
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.components.health_report import HealthReporter
from gensokyoai.schemas.brain_schema import BrainThinkEffort


def _reporter(memory) -> HealthReporter:
    """组一个指标喂食器（memory 鸭子类型：只需 work/long_mem_size）"""
    return HealthReporter(SessionManager(), memory, HealthMonitor())


def _memory(work: int = 0, long: int = 0):
    return types.SimpleNamespace(work_mem_size=work, long_mem_size=long)


async def test_record_feeds_counters_as_metrics():
    """回合计数器真正落成指标（曾把整本 dict 塞 record() 被静默丢弃）"""
    reporter = _reporter(_memory())
    await reporter.record(BrainThinkEffort.LOW, time.monotonic())

    health = reporter._health
    assert (await health.get_metric_history("turn.count"))[-1].value == 1.0
    # 档位分布由 effort.<档位> 指标聚合而来 —— 修复前恒为空
    assert health.reasoning_distribution() == {"low": 1.0}
    assert (await health.get_metric_history("memory.work_size"))[-1].value >= 0.0
    assert (await health.get_metric_history("memory.long_size"))[-1].value >= 0.0
    assert (await health.get_metric_history("turn.tokens"))[-1].value >= 0.0


async def test_record_memory_alert_actually_fires():
    """记忆规模超限时真的告警（阈值表早就配了，修复前从未触发）"""
    reporter = _reporter(_memory(work=600, long=10))
    await reporter.record(BrainThinkEffort.MID, time.monotonic())

    alerts = [
        a for a in reporter._health.alerts() if a.metric and a.metric.name == "memory.work_size"
    ]
    assert alerts, "work_mem_size=600 超过阈值 500 应告警"
    assert alerts[-1].metric.value == 600.0

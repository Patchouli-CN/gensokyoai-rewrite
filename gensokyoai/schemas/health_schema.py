"""健康监控数据契约"""

from typing import Any

import msgspec


class HealthMetric(msgspec.Struct):
    """单个健康指标"""

    name: str
    """ 指标名，如 "inference_latency_ms" """
    value: float
    """ 指标值 """
    unit: str = ""
    """ 单位，如 "ms" / "tok" / "条" """
    timestamp: float = 0.0
    """ 采样时间（Unix 秒） """
    tags: dict[str, str] = msgspec.field(default_factory=dict)
    """ 附加标签，如 {"owner": "brain"} """


class HealthReport(msgspec.Struct):
    """健康状态汇报"""

    functional: str
    """ 来自哪个功能 """
    report_detail: dict[str, Any] = msgspec.field(default_factory=dict)
    """ 报告详细 """
    timestamp: float = 0.0
    """ 报告生成时间 """


class HealthAlert(msgspec.Struct, frozen=True):
    """健康告警"""

    level: str
    """ 告警级别：INFO / WARNING / CRITICAL """
    source: str
    """ 告警来源 """
    message: str
    """ 告警内容 """
    metric: HealthMetric | None = None
    """ 关联指标（可选） """
    timestamp: float = 0.0
    """ 告警时间 """

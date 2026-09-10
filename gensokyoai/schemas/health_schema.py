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


class MetricThreshold(msgspec.Struct, frozen=True):
    """指标告警阈值（**带方向**）。

    旧实现一律用 `value >= threshold`，对「越低越坏」的指标（如可用余量）
    会把「越健康越告警」判反；这里显式区分方向。
    """

    name: str
    """ 指标名 """
    threshold: float
    """ 阈值 """
    lower_is_worse: bool = False
    """ True 表示越低越坏（低于阈值才告警）；False 表示越高越坏 """
    critical_factor: float = 2.0
    """ 越高越坏时，超过阈值的该倍数升为 CRITICAL """
    unit: str = ""
    """ 单位（仅用于告警文案）"""


class MetricSummary(msgspec.Struct, frozen=True):
    """指标聚合摘要 —— 替代把最多 100 条原始样本全塞进报告"""

    name: str
    """ 指标名 """
    count: int = 0
    """ 样本数 """
    last: float = 0.0
    """ 最近一次取值 """
    avg: float = 0.0
    """ 均值 """
    minimum: float = 0.0
    """ 最小值 """
    maximum: float = 0.0
    """ 最大值 """
    p95: float = 0.0
    """ 95 分位 """

"""世界职责组件 —— 从主循环（roleplay/loop.py）拆出的有状态协作者。

每个组件收编主循环里的一个职责簇：独占自己的状态、可独立构造与测试，
由 TouhouWorld 在装配层注入协作者（responder / memory / health ...）。
"""

from .effort import EffortGovernor
from .gate_ctl import System1Gate
from .health_report import HealthReporter
from .parrot import ParrotGuard
from .stall import StallSpeaker

__all__ = [
    "EffortGovernor",
    "HealthReporter",
    "ParrotGuard",
    "StallSpeaker",
    "System1Gate",
]

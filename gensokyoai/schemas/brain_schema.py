"""Brain 决策层的数据契约"""

from enum import Enum
from typing import Literal

import msgspec

from .memory_schema import MemoryItem


class BrainThinkEffort(Enum):
    """模型大脑推理力度"""

    OFF = "off"  # 快速路径，几乎不思考，直接透传
    LOW = "low"  # 1轮思考
    MID = "mid"  # 3轮思考
    HIGH = "high"  # 5轮思考
    MAX = "max"  # 不限制轮数（由用户强制结束或达到 tokens 上限）


Verdict = Literal["pass_through", "draft"]
""" pass_through: 无需初稿 Responder 直接生成; draft: 使用 Brain 初稿 """


class ReasoningStep(msgspec.Struct, frozen=True):
    """单轮思考的中间状态（用于日志和状态追踪）"""

    round: int = 0
    """ 当前思考轮数 """
    thought: str = ""
    """ 本轮的核心思考内容 """
    need_continue_think: bool = False
    """ 是否请求下一轮思考 """
    action_hint: str | None = None
    """ 当前已得出的行动指令（供 Responder 用） """
    intent: str = ""
    """ 当前总结的意图 """
    emotion: str = ""
    """ 当前总结的情绪 """
    confidence: float = 0.5
    """ 当前置信度 """


class BrainConclusion(msgspec.Struct, frozen=True):
    """Brain 产出给 Responder 的结构化结论（事件总线核心载荷，架构文档 §8.2）"""

    verdict: Verdict = "pass_through"
    """ 结论类型 """
    intent: str = ""
    """ 意图判断，如 "回应打招呼" """
    emotion: str = ""
    """ 情绪提示，如 "友好" """
    draft: str | None = None
    """ 行动指令（verdict=draft 时才有）"""
    memory_refs: list[MemoryItem] = []
    """ 需要调用的记忆条目 """
    confidence: float = 0.0
    """ 置信度 (0.0 ~ 1.0) """
    effort: BrainThinkEffort = BrainThinkEffort.OFF
    """ 使用的推理档位 """
    ooc_flag: bool = False
    """ OOC 检测是否触发 """
    reasoning: str | None = None
    """ 压缩后的思考摘要（多轮迭代结果），仅供记忆存档 """
    timestamp: float = 0.0
    """ 产出时间 """


class OOCVerdict(msgspec.Struct, frozen=True):
    """OOC 检测结论"""

    is_ooc: bool = False
    """ 是否判定 OOC """
    confidence: float = 0.0
    """ 判定置信度 """
    reason: str = ""
    """ 判定理由 """
    suggested_rewrite: str | None = None
    """ 建议的改写文本 """

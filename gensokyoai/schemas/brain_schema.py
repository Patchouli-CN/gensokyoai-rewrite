""" Brain 决策层的数据契约 """

from enum import Enum
from typing import Literal

import msgspec

from .memory_schema import MemoryItem

class BrainThinkEffort(Enum):
    """ 模型大脑推理力度 """

    OFF = "off"   # 快速路径，几乎不思考，直接透传
    LOW = "low"   # 简单意图识别
    MID = "mid"   # 意图 + 基础情绪/状态检查
    HIGH = "high" # 完整推理链：意图 + 记忆检索 + 情绪推理 + OOC初检
    MAX = "max"   # 深度思考：完整 CoT + 知识库检索 + 多重 OOC 审计

Verdict = Literal["pass_through", "draft"]
""" pass_through: 无需初稿 Responder 直接生成; draft: 使用 Brain 初稿 """

class BrainConclusion(msgspec.Struct, frozen=True):
    """ Brain 产出给 Responder 的结构化结论（事件总线核心载荷，架构文档 §8.2）"""
    verdict: Verdict = "pass_through"
    """ 结论类型 """
    intent: str = ""
    """ 意图判断，如 "回应打招呼" """
    emotion: str = ""
    """ 情绪提示，如 "友好" """
    draft: str | None = None
    """ 初稿文本（verdict=draft 时才有）"""
    memory_refs: list[MemoryItem] = []
    """ 需要调用的记忆条目 """
    confidence: float = 0.0
    """ 置信度 (0.0 ~ 1.0) """
    effort: BrainThinkEffort = BrainThinkEffort.OFF
    """ 使用的推理档位 """
    ooc_flag: bool = False
    """ OOC 检测是否触发 """
    reasoning: str | None = None
    """ 压缩后的思考摘要，仅供记忆存档 """
    timestamp: float = 0.0
    """ 产出时间 """

class OOCVerdict(msgspec.Struct, frozen=True):
    """ OOC 检测结论 """
    is_ooc: bool = False
    """ 是否判定 OOC """
    confidence: float = 0.0
    """ 判定置信度 """
    reason: str = ""
    """ 判定理由 """
    suggested_rewrite: str | None = None
    """ 建议的改写文本 """

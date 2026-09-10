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
    """Brain 产出给 Responder 的结构化结论（事件总线核心载荷，架构文档 §8.2）

    **两种「思考」来源分开存**，避免混为一谈：

    - `_reasoning`：**工程实现**的思考 —— 本项目「接力思考」协议累积出的最后一轮 `thought`
    - `_raw_reasoning`：**模型原生 thinking** 段（`think: true` 时才由服务端
      `reasoning_content` 或正文里的 `<think>` 段分离而来；默认 `think: false`，故通常为空）

    两者通过只读属性 `reasoning` / `raw_reasoning` 对外暴露。
    """

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
    _reasoning: str | None = None
    """ 工程实现（接力思考）产出的思考摘要 """
    _raw_reasoning: str | None = None
    """ 模型原生 thinking 段（逐轮拼接；开启 think 时才有） """
    reasoning_steps: list[ReasoningStep] = []
    """ 每轮思考快照（接力思考逐轮记录），供轨迹留档与事后复盘；
        OFF 快速路径与不经过接力思考的调用为空列表 """
    timestamp: float = 0.0
    """ 产出时间 """

    @property
    def reasoning(self) -> str | None:
        """工程实现的思考摘要（对外主用；模型原生 thinking 见 `raw_reasoning`）。

        Returns:
            str | None: 接力思考最后一轮的 thought；无则 None
        """
        return self._reasoning

    @property
    def raw_reasoning(self) -> str | None:
        """模型原生 thinking 文本。

        Returns:
            str | None: 原生思考段（`think: true` 时才有）；否则 None
        """
        return self._raw_reasoning


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

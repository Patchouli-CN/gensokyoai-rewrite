"""推理引擎主循环"""
import time

import msgspec

from ..core.session_manager import SessionManager
from ..schemas.brain_schema import BrainConclusion, BrainThinkEffort
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import Message
from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager
from .ooc_detector import OOCDetector

_route_logger = LoggerManager.get_logger("BRAIN")
""" 档位路由是模块级纯函数，独立持有 logger """

_EMOTION_WORDS = ("难过", "开心", "生气", "伤心", "喜欢", "讨厌", "害怕", "哭", "笑", "感动")
_PLOT_WORDS = ("世界", "本质", "为什么", "记得", "过去", "未来", "剧情", "故事", "命运", "秘密")

_THINK_SYSTEM = (
    "你是角色扮演引擎的决策模块。基于人设、场景与记忆，判断用户意图与情绪，"
    "并产出一条贴合人设的初稿回复。只输出一个 JSON 对象，不要输出任何其他内容：\n"
    '{"intent": "意图", "emotion": "情绪", "draft": "初稿回复", "confidence": 0.0}'
)

def route(snapshot: SceneSnapshot) -> BrainThinkEffort:
    """ 档位路由（架构文档 §6.4）：规则启发式打分，零模型调用。

    Args:
        snapshot: 场景快照

    Returns:
        BrainThinkEffort: 选中的推理档位
    """
    text = snapshot.content
    if not text:
        return BrainThinkEffort.OFF

    score = 0
    if len(text) >= 50:
        score += 1
    if len(text) >= 150:
        score += 1
    if len(snapshot.participants) >= 3:
        score += 1
    score += sum(1 for w in _EMOTION_WORDS if w in text)
    score += 2 * sum(1 for w in _PLOT_WORDS if w in text)

    if score == 0:
        effort = BrainThinkEffort.LOW
    elif score <= 2:
        effort = BrainThinkEffort.MID
    elif score <= 4:
        effort = BrainThinkEffort.HIGH
    else:
        effort = BrainThinkEffort.MAX
    _route_logger.debug(
        f"档位路由: score={score} 档位={effort.value} 输入={snapshot.content[:50]!r} "
        f"长度={len(text)} 参与者={len(snapshot.participants)}"
    )
    return effort

class BrainEngine:
    """ 大脑引擎。

    保持薄编排：推理步骤拆到各子模块，防止淤积成原版 _impl.py 式巨石。
    """

    def __init__(
        self,
        sessions: SessionManager,
        persona: str = "",
        ooc: OOCDetector | None = None,
    ) -> None:
        """ 初始化。

        Args:
            sessions: 上下文隔离层（由 L4 注入）
            persona: 角色人设 system prompt（由 L4 从 roleplay 加载后传入）
            ooc: OOC 检测器，可选；传入则对初稿做前置规则快筛
        """
        self._logger = LoggerManager.get_logger("BRAIN")
        self._sessions = sessions
        self._persona = persona
        self._ooc = ooc

    async def think(
        self,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        effort: BrainThinkEffort = BrainThinkEffort.OFF,
    ) -> BrainConclusion:
        """ 执行推理链，产出结构化结论。

        OFF 走零 token 快速路径；LOW 及以上调模型产出初稿（owner="brain.think"
        无状态调用），解析失败或调用失败自动降级为快速路径。

        Args:
            snapshot: 场景快照
            memories: 编排者检索好的相关记忆
            effort: 推理档位（由 route() 决定）

        Returns:
            BrainConclusion: 结构化结论
        """
        started = time.monotonic()
        self._logger.info(
            f"思考开始: 档位={effort.value} 输入={snapshot.sender}: {snapshot.content!r} "
            f"记忆={len(memories)}条 上下文={len(snapshot.context_snippet)}条"
        )
        if effort is BrainThinkEffort.OFF:
            conclusion = self._fast_path(snapshot)
        else:
            conclusion = await self._think_with_model(snapshot, memories, effort)
            if (
                self._ooc is not None
                and conclusion.draft
                and self._ooc.pre_filter(conclusion.draft).is_ooc
            ):
                self._logger.warning(f"初稿被 OOC 规则拦截，丢弃初稿: {conclusion.draft!r}")
                conclusion = BrainConclusion(
                    verdict="pass_through",
                    intent=conclusion.intent,
                    emotion=conclusion.emotion,
                    confidence=conclusion.confidence,
                    effort=effort,
                    ooc_flag=True,
                    timestamp=conclusion.timestamp,
                )
        self._logger.info(
            f"思考完成: 档位={effort.value} 结论={conclusion.verdict} 意图={conclusion.intent} "
            f"情绪={conclusion.emotion} 置信度={conclusion.confidence:.2f} "
            f"耗时={time.monotonic() - started:.2f}s"
        )
        if conclusion.draft:
            self._logger.debug(f"初稿内容: {conclusion.draft}")
        return conclusion

    async def _think_with_model(
        self,
        snapshot: SceneSnapshot,
        memories: list[MemoryItem],
        effort: BrainThinkEffort,
    ) -> BrainConclusion:
        """ 调模型产出初稿结论，解析失败降级为快速路径 """
        memory_text = "\n".join(f"- [{m.topic}] {m.content}" for m in memories) or "（无相关记忆）"
        context_text = "\n".join(snapshot.context_snippet[-5:]) or "（无上下文）"
        user = (
            f"[人设]\n{self._persona or '（未提供）'}\n"
            f"[场景] {snapshot.sender}: {snapshot.content}\n"
            f"[最近上下文]\n{context_text}\n"
            f"[相关记忆]\n{memory_text}"
        )
        try:
            result = await self._sessions.call(
                "brain.think",
                [
                    Message(role="system", content=_THINK_SYSTEM),
                    Message(role="user", content=user),
                ],
                stateless=True,
                temperature=0.4,
                max_new_tokens=400,
            )
        except Exception:
            self._logger.exception("推理调用失败，降级为快速路径")
            return self._fast_path(snapshot, effort)

        parsed = self._parse_json(result.content)
        if not parsed:
            self._logger.warning(f"推理输出 JSON 解析失败，降级为快速路径: {result.content!r}")
            return self._fast_path(snapshot, effort)

        draft = parsed.get("draft") or None
        return BrainConclusion(
            verdict="draft" if draft else "pass_through",
            intent=str(parsed.get("intent", "")),
            emotion=str(parsed.get("emotion", "")),
            draft=draft,
            memory_refs=list(memories),
            confidence=float(parsed.get("confidence", 0.5)),
            effort=effort,
            timestamp=time.time(),
        )

    def _fast_path(
        self, snapshot: SceneSnapshot, effort: BrainThinkEffort = BrainThinkEffort.OFF,
    ) -> BrainConclusion:
        """ 零 token 快速路径：关键词判断意图，直接透传（保留请求档位标记）"""
        text = snapshot.content
        if any(g in text for g in ("你好", "早上好", "晚上好", "在吗", "hi", "hello")):
            intent, emotion = "回应打招呼", "友好"
        else:
            intent, emotion = "日常闲聊", "平淡"
        return BrainConclusion(
            verdict="pass_through",
            intent=intent,
            emotion=emotion,
            effort=effort,
            timestamp=time.time(),
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        """ 容错解析模型输出中的 JSON 对象，失败返回空 dict """
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            return msgspec.json.decode(text[start:end + 1], type=dict)
        except Exception:
            return {}

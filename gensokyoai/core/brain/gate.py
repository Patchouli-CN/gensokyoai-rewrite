"""JeV 式发言门控 —— 混合架构：规则预筛明显信号，群聊模糊带才交裁判。

移植自 Mist-wu/qqbot（「jev 决定该不该说话」）的思路，按本项目约束改造：

- **裁判可插拔**：`Judge` 是协议，本地模型（`LocalJudge`）与真 jev
  （`TypeSafeJudge`，可选依赖）同一接口，`decide` 逻辑零改动；
- **混合门控**：收尾语 / 私聊 / 被 @ 这类明显信号由零成本规则直接定，
  只有「群里没人点名的新消息」才花一次裁判调用；
- **兜底完整**：裁判缺失或异常时退化为「点名才回」，行为可预期。

防刷屏**不设硬冷却**：把「近窗口内自己说了多少、距上次发言多久」写进
state（`PresenceTracker`），让裁判自己权衡 —— 与 qqbot 一致。
"""

import re
import time
from collections import deque
from typing import Literal, Protocol

import msgspec

from ...schemas.brain_schema import BrainThinkEffort
from ...schemas.scene_schema import SceneSnapshot

GateSource = Literal["rule", "judge", "fallback"]
""" 决定来源：规则直判 / 裁判打分 / 兜底 """


class Question(msgspec.Struct, frozen=True):
    """一道是非题（口径 = instructions 一段话，true/false 描述折叠其中）。

    与 TypeSafe 的 `Noul` 对齐但不依赖其 SDK：`TypeSafeJudge` 负责转换。
    """

    instructions: str


class Judge(Protocol):
    """裁判协议：吃结构化 state + 一组是非题，返回每个问题的 yes 概率（0~1）。"""

    async def ask(
        self, state: dict[str, object], questions: dict[str, Question]
    ) -> dict[str, float]: ...


class GateScores(msgspec.Struct, frozen=True):
    """裁判打分明细（落日志便于调参）"""

    reply: float = 0.0
    addressed: float = 0.0
    search: float = 0.0
    threshold: float = 0.0


class GateDecision(msgspec.Struct, frozen=True):
    """一次门控结论"""

    reply: bool
    source: GateSource
    reason: str
    search: bool = False
    """ 裁判认为需要查证（> search_threshold）；当前仅落日志，预留给工具/档位联动 """
    effort: float | None = None
    """ 裁判的「思考深度」分（needs_deep，0~1）；None = 裁判没给这信息，
        调用方应回落规则 route()。经 tier_from_deep_score 映射为推理档位 """
    scores: GateScores | None = None


class PresenceTracker:
    """滑动窗口内的发言统计：喂给裁判的 bot_activity（防刷屏靠裁判自觉）。"""

    def __init__(self, window_s: float = 300.0, clock=time.monotonic) -> None:
        """初始化。

        Args:
            window_s: 统计窗口秒数（默认 5 分钟，对齐 qqbot 的 presence 窗口）
            clock: 时钟函数（可注入以便测试）
        """
        self._window_s = window_s
        self._clock = clock
        self._events: deque[tuple[float, bool]] = deque()

    def record(self, *, from_bot: bool) -> None:
        """记一条发言（from_bot=True 表示角色自己说的）。"""
        self._events.append((self._clock(), from_bot))
        self._prune()

    def stats(self) -> tuple[int, int, float | None]:
        """窗口内统计。

        Returns:
            tuple: (bot 发言数, 总发言数, 距 bot 上次发言秒数；从未发言为 None)
        """
        self._prune()
        bot_count = sum(1 for _, from_bot in self._events if from_bot)
        last_bot = max((ts for ts, from_bot in self._events if from_bot), default=None)
        idle = None if last_bot is None else max(0.0, self._clock() - last_bot)
        return bot_count, len(self._events), idle

    def _prune(self) -> None:
        cutoff = self._clock() - self._window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()


_BARE_REACTION = re.compile(
    r"^(?:[哈嘿呵嘻hH]+|草+|6+|[嗯恩哦噢喔啊]+|好+的?|好吧|行|ok|收到|懂了|明白了?|谢谢|谢了|多谢|感谢|thx|thanks|拜拜|晚安|[?？!！]+|\[[^\]]+\])+$",
    re.IGNORECASE,
)
""" 收尾语 / 纯反应表达式（移植自 qqbot 的兜底规则，这里用于规则预筛） """


def is_bare_reaction(text: str) -> bool:
    """是否「无需回应」的收尾语 / 纯反应（哈哈哈、草、666、好的、[表情包]……）。

    Args:
        text: 消息文本（可含 @ 段）

    Returns:
        bool: True 表示这句话不值得接
    """
    core = re.sub(r"@\S+", "", text)
    core = re.sub(r"[\s,，.。~～…、]+", "", core)
    return core == "" or _BARE_REACTION.search(core) is not None


def build_state(
    *,
    snapshot: SceneSnapshot,
    bot_name: str,
    persona: str,
    recent: list[str],
    presence: tuple[int, int, float | None],
) -> dict[str, object]:
    """构造裁判输入的结构化 state（对应 qqbot 的 buildState）。

    Args:
        snapshot: 本批待判断的快照
        bot_name: 角色名
        persona: 人设摘要
        recent: 最近对话（旧 -> 新，不含本快照）
        presence: PresenceTracker.stats() 的输出（bot 数, 总数, 距上次发言秒数）

    Returns:
        dict: 序列化友好的 state（JSON 可直接进提示词）
    """
    bot_count, total, idle = presence
    scene = (
        "群聊"
        if snapshot.scene_type == "group_chat"
        else ("私聊" if snapshot.scene_type == "private_chat" else snapshot.scene_type)
    )
    return {
        "chat": scene,
        "bot": {"name": bot_name, "persona": persona},
        "bot_activity": {
            "bot_messages_last_window": bot_count,
            "all_messages_last_window": total,
            "seconds_since_bot_last_spoke": None if idle is None else round(idle),
        },
        "recent_messages": recent,
        "new_message": {
            "from": snapshot.sender,
            "text": snapshot.content,
            "directly_addressed": snapshot.is_direct,
        },
    }


def build_questions(bot_name: str) -> dict[str, Question]:
    """System-1 四问（对齐 qqbot 三问 + 本项目的深度路由一问）。"""
    return {
        "should_reply": Question(
            f"现在该不该由「{bot_name}」回应 new_message？"
            f"是：new_message 在等「{bot_name}」回应，或「{bot_name}」有值得接的话。"
            f"否：这是别人之间的对话、正在收尾（寒暄/附和/表情/语气词），"
            f"或「{bot_name}」接话只会显得吵闹。"
            f"参考 bot_activity：近窗口内自己说得多、离上次发言近时更应谨慎。"
        ),
        "addressed": Question(f"new_message 是否在直接对「{bot_name}」说、期待回应。"),
        "needs_search": Question("好好回应 new_message 是否依赖需要联网查证的最新事实。"),
        "needs_deep": Question(
            "好好回应 new_message 需要多深的思考？"
            "0 = 寒暄/语气词，直觉即可；0.5 = 日常对话，看看关系和情绪；"
            "0.75 = 复杂问题/剧情推进，要完整推演；1 = 重大剧情节点/情感转折，要最深的推演。"
            "消息越长、剧情词越多、越涉及角色关系和过往，越靠近 1。"
        ),
    }


def tier_from_deep_score(
    score: float, cuts: tuple[float, float, float] = (0.3, 0.6, 0.85)
) -> BrainThinkEffort:
    """needs_deep 分数 -> 推理档位（cut = mid/high/max 三个切点）。

    Args:
        score: 裁判给的深度分（0~1）
        cuts: (进 MID 的线, 进 HIGH 的线, 进 MAX 的线)

    Returns:
        BrainThinkEffort: LOW / MID / HIGH / MAX（不给 NONE——空输入由规则层处理）
    """
    cut_mid, cut_high, cut_max = cuts
    if score >= cut_max:
        return BrainThinkEffort.MAX
    if score >= cut_high:
        return BrainThinkEffort.HIGH
    if score >= cut_mid:
        return BrainThinkEffort.MID
    return BrainThinkEffort.LOW


async def decide(
    *,
    snapshot: SceneSnapshot,
    bot_name: str,
    persona: str,
    recent: list[str],
    presence: tuple[int, int, float | None],
    judge: Judge | None,
    group_threshold: float,
    search_threshold: float,
    route_by_model: bool = False,
) -> GateDecision:
    """混合门控主入口：规则预筛 -> 裁判打分 -> 兜底。

    Args:
        snapshot: 待判断快照
        bot_name: 角色名
        persona: 人设摘要（进 state）
        recent: 最近对话（旧 -> 新）
        presence: 活跃度统计（见 PresenceTracker.stats）
        judge: 裁判；None 表示无裁判（直接走兜底）
        group_threshold: 群聊未点名时 should_reply 的发言阈值
        search_threshold: needs_search 超过该值视为「可能要查证」
        route_by_model: 是否连「私聊 / 被 @」也问裁判——这些场景回复与否
            由规则直判，但**档位路由**仍想由模型说了算（System-1 报文两用）

    Returns:
        GateDecision: 是否发言 + 来源 + 原因 + 深度分 / 打分明细
    """
    # 1) 反射弧：收尾语 / 纯反应，零成本跳过（这条连裁判都不问）
    if is_bare_reaction(snapshot.content):
        return GateDecision(reply=False, source="rule", reason="收尾语/纯反应，无需回应")

    # 2) 直连信号：私聊 / 被 @ 必回；模型路由开启时仍会为「想多深」问一次裁判
    forced_reply = snapshot.scene_type == "private_chat" or snapshot.is_direct

    # 3) System-1 模型层：群聊模糊带（门控需要）或任何想模型路由的回合都问
    if judge is not None and (route_by_model or not forced_reply):
        try:
            answers = await judge.ask(
                build_state(
                    snapshot=snapshot,
                    bot_name=bot_name,
                    persona=persona,
                    recent=recent,
                    presence=presence,
                ),
                build_questions(bot_name),
            )
        except Exception as error:  # 裁判故障不拖死主链路，退化为规则
            if forced_reply:
                return GateDecision(
                    reply=True, source="rule", reason=f"裁判异常，直连信号优先: {error}"
                )
            return _fallback(snapshot, bot_name, recent, f"裁判异常: {error}")

        reply_score = _probability(answers, "should_reply")
        search_score = _probability(answers, "needs_search")
        deep_score = _optional_probability(answers, "needs_deep")
        yes = forced_reply or reply_score >= group_threshold
        comparator = "≥" if reply_score >= group_threshold else "<"
        if forced_reply:
            reason = "直连信号（裁判打分仅供路由参考）"
        else:
            reason = f"should_reply={reply_score:.2f} {comparator} 阈值 {group_threshold:.2f}"
        return GateDecision(
            reply=yes,
            source="judge",
            reason=reason,
            search=search_score > search_threshold,
            effort=deep_score,
            scores=GateScores(
                reply=reply_score,
                addressed=_probability(answers, "addressed"),
                search=search_score,
                threshold=group_threshold,
            ),
        )

    # 4) 无裁判（或无需裁判）：直连信号放行，其余「点名才回」兜底
    if forced_reply:
        return GateDecision(
            reply=True,
            source="rule",
            reason="私聊消息" if snapshot.scene_type == "private_chat" else "被 @ 或被回复",
        )
    return _fallback(snapshot, bot_name, recent, "无裁判")


def _fallback(snapshot: SceneSnapshot, bot_name: str, recent: list[str], why: str) -> GateDecision:
    """兜底规则：名字被提及才接话（私聊/被 @ 已在规则层放过，不会到这）。"""
    mentioned = bool(bot_name) and (
        bot_name in snapshot.content or any(bot_name in line for line in recent)
    )
    return GateDecision(
        reply=mentioned,
        source="fallback",
        reason=f"{why}；{'名字被提及' if mentioned else '群聊闲聊，不接'}",
    )


def _probability(answers: dict[str, float], name: str, default: float = 0.0) -> float:
    """从裁判答案里取一个 0~1 的概率，缺失/非法给默认值。"""
    value = answers.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(1.0, max(0.0, float(value)))


def _optional_probability(answers: dict[str, float], name: str) -> float | None:
    """取一个 0~1 的概率；**键缺失**返回 None（区别于裁判明确打了 0 分）。"""
    if name not in answers:
        return None
    value = answers[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(1.0, max(0.0, float(value)))

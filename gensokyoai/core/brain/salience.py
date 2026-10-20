"""显著性打分 —— 丘脑门控（thalamic gating）：感觉先过显著性过滤，再决定谁进皮层。

零成本规则打分（无模型调用），一处打分三处生效：

1. **合并折叠**（satori/queue.py）：连续低分事件折叠成「（K 条附和）」计数标记，
   积压有 token 上界；
2. **记忆分级**（roleplay/loop.py）：显著性 -> importance，附和低分（淘汰时物理
   删除、不进长期记忆、不做 embedding 语义关联）；
3. **精力调制**：精力低时折叠阈值上浮（累了只听重点，只抬不压）。

**不含复读 / 已答判定**——群卫生钦定在平台侧插件（nb2_plugin 的 RepeatGuard /
AnsweredGuard），core 不重复造。
"""

from collections.abc import Callable

from ..config import AttentionSettings
from .gate import is_bare_reaction

EMOTION_WORDS = ("难过", "开心", "生气", "伤心", "喜欢", "讨厌", "害怕", "哭", "笑", "感动")
""" 情感词表（engine.route 与显著性打分共用，集中定义避免两处漂移） """

PLOT_WORDS = ("世界", "本质", "为什么", "记得", "过去", "未来", "剧情", "故事", "命运", "秘密")
""" 剧情词表（同上） """

NOISE_SCORE = 0.05
""" 收尾语 / 纯反应的显著性（恒低于默认折叠阈值） """

BASE_SCORE = 0.4
""" 普通消息的基础显著性（高于默认折叠阈值 0.3，正常对话不折叠） """


def score_event(content: str, *, is_direct: bool = False) -> float:
    """单条消息的显著性（0~1）。

    直连（被 @ / 被回复）恒 1.0；收尾语 / 纯反应（哈哈、草、好的……）恒 0.05；
    其余基础 0.4，按长度 / 问句 / 情感剧情词阶梯加分，封顶 1.0。

    Args:
        content: 消息文本
        is_direct: 是否直接针对角色

    Returns:
        float: 显著性分数（0~1）
    """
    if is_direct:
        return 1.0
    if is_bare_reaction(content):
        return NOISE_SCORE
    score = BASE_SCORE
    if len(content) >= 50:
        score += 0.2
    if len(content) >= 150:
        score += 0.2
    if "？" in content or "?" in content:
        score += 0.15
    score += min(0.2, 0.1 * sum(1 for w in EMOTION_WORDS if w in content))
    score += min(0.3, 0.15 * sum(1 for w in PLOT_WORDS if w in content))
    return min(1.0, score)


def importance_of(score: float, *, is_direct: bool, fold_threshold: float) -> float:
    """显著性 -> 记忆 importance 映射。

    - 直连 0.6：写入即达长期记忆归档线（被点名的互动值得记住）；
    - 低于折叠阈值 0.2：淘汰时物理删除，不进长期记忆、不上关联图；
    - 其余 0.5：与旧默认一致。

    Args:
        score: 显著性分数
        is_direct: 是否直接针对角色
        fold_threshold: 当前折叠阈值（可能已被精力调制）

    Returns:
        float: 记忆 importance（0.2 / 0.5 / 0.6 三档）
    """
    if is_direct:
        return 0.6
    if score < fold_threshold:
        return 0.2
    return 0.5


class SalienceScorer:
    """显著性打分器：持有配置与（可选）精力回调，合并折叠与记忆分级共用。"""

    def __init__(self, settings: AttentionSettings) -> None:
        """初始化。

        Args:
            settings: 显著性分层配置
        """
        self._s = settings
        self._energy: Callable[[], float] | None = None

    def bind_energy(self, energy: Callable[[], float]) -> None:
        """绑定精力回调（世界装配时注入）；未绑定 = 阈值不调制。

        Args:
            energy: 返回当前精力值（0~1）的回调
        """
        self._energy = energy

    @property
    def keep_verbatim(self) -> int:
        """合并时末尾强制保留全文的事件数。"""
        return self._s.keep_verbatim

    def fold_threshold(self) -> float:
        """当前折叠阈值：绑定精力且联动开启时按 (1-精力)*span 上浮（只抬不压）。"""
        base = self._s.fold_threshold
        if not self._s.energy_modulate or self._energy is None:
            return base
        return min(1.0, base + (1.0 - self._energy()) * self._s.energy_span)

    def score(self, content: str, *, is_direct: bool = False) -> float:
        """单条消息的显著性（见 `score_event`）。"""
        return score_event(content, is_direct=is_direct)

    def importance(self, content: str, *, is_direct: bool = False) -> float:
        """一条用户消息的记忆 importance（打分 + 当前阈值一次完成）。"""
        return importance_of(
            self.score(content, is_direct=is_direct),
            is_direct=is_direct,
            fold_threshold=self.fold_threshold(),
        )

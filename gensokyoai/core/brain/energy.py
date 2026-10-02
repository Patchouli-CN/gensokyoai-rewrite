"""精力模型 —— HumanLikeSystem 阶段二：把「说太多会累、冷场会懒、深夜会困」
量化成一个 0~1 的精力值，调制发言门槛与回复长度。

三个因子（场景无关机制，群聊语义留在平台/插件层）：

- **存在感惩罚**：滑动窗口内自己发言占比超过免罚线后开始扣精力（话说多了会累）；
- **冷场退避**：连续「本可接却没接」的回合按几何衰减削精力（越来越懒得开口），
  一开口即复位——反射弧级的跳过（收尾语/纯反应）不算数；
- **生物钟**：本地时间深夜时段精力打折（困了话少）。

精力只**调制**不**否决**：群聊模糊带的发言阈值随精力降低而抬高，回复长度经
`[当前状态]` 提示传给 Responder；私聊 / 被 @ 的直连信号不受阈值影响。

灵感来自 MaiBot 的 reply_necessity（存在感惩罚/压力分）与 idle_backoff（空闲
退避），这里收敛成单变量「精力」：好调参、好解释、好落日志。
"""

import time
from collections.abc import Callable
from datetime import datetime

from ..config import EnergySettings
from .gate import GateSource, PresenceTracker

_SKIP_SOURCES = frozenset({"judge", "fallback"})
""" 计入冷场退避的跳过来源：裁判/兜底说「不接」是主动选择；
规则直跳（收尾语反射弧）不算——被「哈哈」跳过一次不该消耗精力 """


class EnergyModel:
    """精力跟踪器：吃 PresenceTracker 统计 + 跳过/开口事件，产出精力值。

    无状态部分（存在感）直接读 PresenceTracker，有状态部分（冷场连胜）自维护；
    时钟与本地时间都可注入，测试友好。
    """

    def __init__(
        self,
        settings: EnergySettings,
        presence: PresenceTracker,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        """初始化。

        Args:
            settings: 精力模型配置
            presence: 活跃度统计（与门控共用同一个 PresenceTracker）
            clock: 单调时钟（预留；存在感窗口由 presence 自己的时钟管）
            now: 本地时间函数（生物钟用；测试可注入固定时刻）
        """
        self._s = settings
        self._presence = presence
        self._clock = clock
        self._now = now
        self._skip_streak = 0

    @property
    def skip_streak(self) -> int:
        """当前冷场连胜（连续「本可接却没接」的回合数）。"""
        return self._skip_streak

    def note_skip(self, source: GateSource) -> None:
        """记一次「没接话」：仅裁判/兜底的主动跳过累积冷场退避。

        Args:
            source: 门控决定来源（rule 反射弧跳过不计）
        """
        if source in _SKIP_SOURCES:
            self._skip_streak = min(self._skip_streak + 1, self._s.skip_streak_cap)

    def note_reply(self) -> None:
        """记一次开口：冷场退避立即复位（说过话就不算冷场）。"""
        self._skip_streak = 0

    def factors(self) -> tuple[float, float, float]:
        """三个因子各自取值（1.0 = 无影响），供日志/测试拆解。

        Returns:
            tuple: (生物钟, 存在感, 冷场退避)
        """
        return self._circadian_factor(), self._presence_factor(), self._backoff_factor()

    def energy(self) -> float:
        """当前精力值（0~1，三因子连乘）。"""
        circadian, presence, backoff = self.factors()
        return round(min(1.0, max(0.0, circadian * presence * backoff)), 4)

    def modulate_threshold(self, base: float) -> float:
        """精力越低，群聊模糊带的接话门槛抬得越高（精力只抬不压，封顶 cap）。

        Args:
            base: 配置的基准阈值（gate.group_threshold）

        Returns:
            float: 调制后的阈值（精力满格时等于 base）
        """
        lifted = base + (1.0 - self.energy()) * self._s.threshold_span
        return min(self._s.threshold_cap, max(base, lifted))

    def verbosity_hint(self) -> str:
        """低精力时给 Responder 的简短提示；精力充沛返回空串（不注入）。"""
        if self.energy() >= self._s.brief_below:
            return ""
        return "当前精力偏低（疲倦/话少）：回复尽量简短，一两句话内说完，语气可以慵懒些。"

    def describe(self) -> str:
        """一行日志用的拆解：精力值 + 三因子 + 冷场连胜。"""
        circadian, presence, backoff = self.factors()
        return (
            f"energy={self.energy():.2f} (生物钟={circadian:.2f} 存在感={presence:.2f} "
            f"退避={backoff:.2f} 冷场={self._skip_streak})"
        )

    def _circadian_factor(self) -> float:
        """生物钟：深夜时段（可跨午夜）精力打折。"""
        s = self._s
        if s.night_factor >= 1.0 or s.night_start == s.night_end:
            return 1.0
        hour = self._now().hour
        if s.night_start < s.night_end:  # 不跨午夜，如 1~7
            in_night = s.night_start <= hour < s.night_end
        else:  # 跨午夜，如 23~7
            in_night = hour >= s.night_start or hour < s.night_end
        return s.night_factor if in_night else 1.0

    def _presence_factor(self) -> float:
        """存在感惩罚：窗口内自己发言占比超免罚线后线性扣，扣完为止。"""
        s = self._s
        bot, total, _idle = self._presence.stats()
        free = min(1.0, max(0.0, s.presence_free_ratio))
        if total <= 0 or s.presence_penalty <= 0 or free >= 1.0:
            return 1.0
        ratio = bot / total
        if ratio <= free:
            return 1.0
        over = min(1.0, (ratio - free) / (1.0 - free))  # 0~1：超出免罚线的程度
        return max(0.0, 1.0 - s.presence_penalty * over)

    def _backoff_factor(self) -> float:
        """冷场退避：连续不接话按几何衰减削精力。"""
        return self._s.skip_decay**self._skip_streak

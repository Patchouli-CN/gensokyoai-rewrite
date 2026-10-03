"""深思考过渡语 —— 掩盖接力思考延迟的那一句角色口吻的话。

多轮接力思考的延迟肉眼可见，先垫一句「唔……让妾身想想」能让等待自然化。
三重门控防止人机感：值得档位（HIGH/MAX）+ 非首回合 + 冷却轮数 + 时间
间隔 + 概率掷骰。

过渡语只投递显示层，**不写记忆**（对 Brain 是噪音）；但它进了 responder
有状态会话，正式回复能看到它、自然承接不重复。
"""

import random
import time

from ...core.responder.generator import Responder
from ...mouth.base import Mouth
from ...schemas.brain_schema import BrainThinkEffort
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ...utils.text import strip_control_chars

_STALL_EFFORTS = {BrainThinkEffort.HIGH, BrainThinkEffort.MAX}
""" 值得垫过渡语的档位：多轮接力思考，延迟肉眼可见 """


class StallSpeaker:
    """过渡语播报：门控（档位/冷却/概率）+ 生成 + 投递 + 冷却记账。"""

    def __init__(
        self,
        responder: Responder,
        mouth: Mouth,
        character_name: str,
        *,
        probability: float = 0.6,
        cooldown_turns: int = 3,
        min_interval: float = 180.0,
    ) -> None:
        """初始化。

        Args:
            responder: 表达层；过渡语与正式回复共用同一个有状态会话
            mouth: 口层；过渡语只进显示层，不进记忆
            character_name: 角色名（投递时的说话者署名）
            probability: 深思考前垫过渡语的概率（0 关闭该行为）
            cooldown_turns: 两次过渡语之间的最小回合间隔
            min_interval: 两次过渡语之间的最小时间间隔（秒）
        """
        self._logger = LoggerManager.get_logger("STALL")
        self._responder = responder
        self._mouth = mouth
        self._character_name = character_name
        self._probability = probability
        self._cooldown_turns = cooldown_turns
        self._min_interval = min_interval
        self.last_turn = -(10**9)
        """ 上次垫过渡语的回合号（冷却门控用；可持久化，见 dump/load）"""
        self._last_time = float("-inf")
        """ 上次垫过渡语的时刻（monotonic；跨进程无意义，不持久化）"""

    def should_stall(self, effort: BrainThinkEffort, turn: int) -> bool:
        """是否值得垫过渡语：仅深思考档、非开局、出了冷却期、再掷中概率。

        Args:
            effort: 本回合推理档位
            turn: 当前回合号（从 1 起）

        Returns:
            bool: True 表示先垫一句过渡语
        """
        if effort not in _STALL_EFFORTS:
            return False
        if turn <= 1:
            return False
        if turn - self.last_turn < self._cooldown_turns:
            return False
        if time.monotonic() - self._last_time < self._min_interval:
            return False
        # 行为抖动采样，非安全用途——保留 S311 对将来加密误用的拦截
        return random.random() < self._probability  # noqa: S311

    async def maybe_stall(
        self, snapshot: SceneSnapshot, effort: BrainThinkEffort, turn: int
    ) -> None:
        """深思考前垫一句角色口吻过渡语（门控见 should_stall）。

        生成失败只记日志（跳过，不影响主链路）。

        Args:
            snapshot: 触发深度思考的场景快照
            effort: 本回合推理档位
            turn: 当前回合号
        """
        if not self.should_stall(effort, turn):
            return
        try:
            line = await self._responder.stall(snapshot)
        except Exception:
            self._logger.exception("过渡语生成失败（跳过，不影响主链路）")
            return
        if not line:
            return
        self.last_turn = turn
        self._last_time = time.monotonic()
        await self._mouth.send(self._character_name, strip_control_chars(line))
        self._logger.info(f"过渡语已投递: {line!r}")

    # ---------------------------------------------------------------- 持久化

    def dump(self) -> int:
        """导出上次垫过渡语的回合号（供 world_runtime codec 编排）。

        Returns:
            int: 上次垫话的回合号；从未垫过为极大的负数
        """
        return self.last_turn

    def load(self, turn: object) -> None:
        """从存档恢复回合号（非法值保持默认）；monotonic 时刻重置为「现在」。

        Args:
            turn: dump() 产出的回合号；None/非 int 时跳过
        """
        if isinstance(turn, int):
            self.last_turn = turn
        # monotonic 时刻跨进程无意义：以「现在」为起点重新计时最小间隔
        self._last_time = time.monotonic()

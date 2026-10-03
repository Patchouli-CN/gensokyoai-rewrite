"""System-1 发言门控接线 —— 世界回合入口的「该不该接话 + 该想多深」。

纯判决逻辑在 core/brain/gate.py（规则直判 -> 裁判 -> 兜底）；本组件持有
门控装配状态（配置 / 裁判 / 活跃度统计 / 精力模型 / 人设摘要），把它们缝成
一次 decide 调用：精力调制阈值、拼裁判 state、落带分明细的日志。

**跳过时的副作用不在本组件**：写记忆 / 喂 gate.skip 计数是总线与任务管理器
的事，归世界（装配层）。本组件只做「无副作用的判决 + 日志」。
"""

from ...core.brain.energy import EnergyModel
from ...core.brain.gate import GateDecision, Judge, PresenceTracker
from ...core.brain.gate import decide as core_decide
from ...core.config import GateSettings
from ...core.memorizer.manager import MemoryManager
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager


class System1Gate:
    """发言门控装配：配置 + 裁判 + 活跃度 + 精力，缝成一次无副作用判决。"""

    def __init__(
        self,
        gate: GateSettings,
        judge: Judge | None,
        presence: PresenceTracker,
        energy: EnergyModel | None,
        persona_brief: str,
        bot_name: str,
        memory: MemoryManager,
    ) -> None:
        """初始化。

        Args:
            gate: 门控配置（阈值 / 路由开关 / 裁判参数）
            judge: 裁判实例（None = 纯规则 + 兜底，不调模型）
            presence: 活跃度统计（与主循环/口层共用同一个实例）
            energy: 精力模型（None = 不跟踪；调制群聊模糊带阈值）
            persona_brief: 人设摘要（进裁判 state；完整人设留给 Responder）
            bot_name: 角色名（进口径与 state）
            memory: 记忆管理器（取最近对话进裁判 state）
        """
        self._logger = LoggerManager.get_logger("GATE")
        self._gate = gate
        self._judge = judge
        self._presence = presence
        self._energy = energy
        self._persona_brief = persona_brief
        self._bot_name = bot_name
        self._memory = memory

    @property
    def settings(self) -> GateSettings:
        """门控配置（世界的启动日志 / 兜底判断读它）。"""
        return self._gate

    def should_consult(self) -> bool:
        """有裁判、且（开了门控 或 开了模型路由）时才问裁判。

        Returns:
            bool: True 表示本回合要走一次 decide
        """
        return self._judge is not None and (self._gate.enabled or self._gate.route_by_model)

    async def decide(self, snapshot: SceneSnapshot, turn: int) -> GateDecision:
        """只做 decide + 日志（无副作用；跳过记账由调用方按门控开关决定）。

        Args:
            snapshot: 待判断快照
            turn: 当前回合号（仅日志用）

        Returns:
            GateDecision: 是否发言 + 来源 + 原因 + 深度分 / 打分明细
        """
        recent = await self._memory.recent(5)
        recent_texts = [m.content for m in reversed(recent)]
        threshold = self._gate.group_threshold
        energy_text = ""
        if self._energy is not None:
            # 精力调制：说多了/冷场久/深夜 -> 群聊模糊带阈值抬高（只抬不压）
            threshold = self._energy.modulate_threshold(threshold)
            energy_text = f" {self._energy.describe()}"
        decision = await core_decide(
            snapshot=snapshot,
            bot_name=self._bot_name,
            persona=self._persona_brief,
            recent=recent_texts,
            presence=self._presence.stats(),
            judge=self._judge,
            group_threshold=threshold,
            search_threshold=self._gate.search_threshold,
            route_by_model=self._gate.route_by_model,
        )
        scores = (
            f" reply={decision.scores.reply:.2f} addressed={decision.scores.addressed:.2f}"
            f" search={decision.scores.search:.2f} threshold={decision.scores.threshold:.2f}"
            if decision.scores is not None
            else ""
        )
        deep = f" deep={decision.effort:.2f}" if decision.effort is not None else ""
        self._logger.info(
            f"[gate] 回合{turn} {'REPLY' if decision.reply else 'skip'} "
            f"({decision.source}: {decision.reason}){scores}{deep}{energy_text} :: "
            f"{snapshot.sender}: {snapshot.content[:60]}"
        )
        return decision

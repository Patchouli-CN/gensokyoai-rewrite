"""主动发言 —— 冷场时角色自己冒泡的那个节拍（生物钟住户）。

**零思考 token**：不经过 Brain，四维规则评估对话欲（纯函数在
`roleplay/initiative.py`），达标就用 pass_through 规则结论驱动
DeliveryService 开口——与主循环同一套投递，但不做 OOC 硬规则守门
（自己发起的话题，防复读与可疑标记照跑，它也是「说话」）。

与主循环的协作全走 RuntimeState：忙时避让、回写前校验证世代、
开口后刷新 last_activity；记忆投递走事件总线（侧链落盘与主循环同路径）。
"""

import time

from ...core.event_bus import EventBus
from ...core.memorizer.manager import MemoryManager
from ...schemas.brain_schema import BrainConclusion
from ...schemas.event_schema import EventTopic
from ...schemas.memory_schema import MemoryItem, MemoryType
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ..character import Character
from ..initiative import describe_silence, evaluate_initiative
from ..runtime import RuntimeState
from .delivery import DeliveryService


class InitiativeSpeaker:
    """主动发言节拍：空闲判定 -> 对话欲评估 -> 规则结论 -> 开口。"""

    def __init__(
        self,
        character: Character,
        memory: MemoryManager,
        bus: EventBus,
        runtime: RuntimeState,
        delivery: DeliveryService,
        *,
        urge_threshold: float = 0.35,
        idle_threshold: float = 180.0,
        is_stopping,
    ) -> None:
        """初始化。

        Args:
            character: 角色（卡权重 / 表达欲基线 / 情绪进结论；对话欲回写 status）
            memory: 记忆管理器（取最近对话评估 + 开口后回写）
            bus: 事件总线（主动发言的记忆投递，走 MEMORY_WRITE）
            runtime: 跨回合运行时状态（busy 避让 / 世代校验 / 活跃度刷新）
            delivery: 说话通道（开口与主循环同一套投递）
            urge_threshold: 对话欲阈值，达到才开口
            idle_threshold: 触发评估的最小空闲（秒）
            is_stopping: 关闭流程判定（读感知器的停止请求）
        """
        self._logger = LoggerManager.get_logger("INITIATIVE")
        self._character = character
        self._memory = memory
        self._bus = bus
        self._runtime = runtime
        self._delivery = delivery
        self._urge_threshold = urge_threshold
        self._idle_threshold = idle_threshold
        self._is_stopping = is_stopping

    async def tick(self) -> None:
        """一次节拍（生物钟周期调用）：空闲超阈值且值得说，才评估开口。

        忙 / 关闭中 / 未达空闲门槛都直接返回；评估异常只记日志——
        定时任务不该把心跳拖死。
        """
        if self._runtime.busy or self._is_stopping():
            return
        idle = self._runtime.idle_for()
        if idle < self._idle_threshold:
            return

        try:
            await self._speak(idle)
        except Exception:
            self._logger.exception("主动发言评估失败")

    async def _speak(self, idle: float) -> None:
        """评估对话欲并尝试主动开口。

        Args:
            idle: 距上次互动的空闲秒数（调用方已过门槛）
        """
        gen = self._runtime.generation
        recent = await self._memory.recent(6)
        recent_texts = [m.content for m in reversed(recent)]
        urge = evaluate_initiative(
            recent_texts,
            character_name=self._character.name,
            weights=self._character.card.motivation_weights,
            idle_seconds=idle,
            expression_base=self._character.card.expression_base,
        )
        self._character.status.update(motivation=round(urge, 2))
        if urge < self._urge_threshold:
            return
        if self._runtime.busy or gen != self._runtime.generation:
            return

        self._logger.info(f"主动发言触发: 对话欲={urge:.2f} 空闲={idle:.0f}s")
        with self._runtime.begin_busy():
            snapshot = SceneSnapshot(
                scene_type="group_chat",
                sender="环境",
                content=describe_silence(recent_texts, idle_seconds=idle),
                is_direct=False,
                context_snippet=[m.content for m in reversed(recent)][-5:],
                timestamp=time.time(),
            )
            conclusion = BrainConclusion(
                verdict="pass_through",
                intent="主动发起话题",
                emotion=self._character.status.emotion,
            )
            # 与主循环同一套投递：口层支持流式则逐块显示，否则缓冲投递（主动发言不做 OOC 守门）
            reply = await self._delivery.deliver(snapshot, conclusion, recent, ooc_guard=False)

        char_mem = MemoryItem(
            topic="对话",
            content=f"{self._character.name}: {reply}",
            memory_type=MemoryType.DIALOGUE,
        )
        await self._bus.publish(
            EventBus.new(EventTopic.MEMORY_WRITE, source="initiative", payload=char_mem)
        )
        self._runtime.note_activity()

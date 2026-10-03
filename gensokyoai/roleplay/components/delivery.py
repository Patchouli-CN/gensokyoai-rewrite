"""表达 + 投递管线 —— 主循环与主动发言共用的「说话」通道。

一进一出：进（场景快照 + Brain 结论 + 检索记忆），出（完整回复文本）。
中间是固定流程：

1. **事前提示**：防复读预防提示（`ParrotGuard.avoid_hint`）+ 精力状态提示
   （低精力长话短说）——两条路径都生效，流式已投递无从改起，只能事前防；
2. **生成投递二选一**：口层支持流式且 blocking 闸门没生效 -> 逐块流式；
   否则缓冲生成。blocking 生效时**放弃流式**——回复必须先完整生成、过
   System-1 出戏审查才放行（逐字蹦的手感换「说出口的话都过了审」）；
3. **出口守门**（仅缓冲路径）：blocking 审查 -> 硬规则守门 -> 防复读纠偏，
   之后可疑短回复标记 + 收尾去重；
4. **状态回填**：presence 记一次发言、精力复位、文风窗口落本轮回复。

所有协作者由装配层注入；本类只做编排，不持有跨回合状态。
"""

from ...core.brain.energy import EnergyModel
from ...core.brain.gate import PresenceTracker
from ...core.responder.generator import Responder
from ...mouth.base import Mouth
from ...schemas.brain_schema import BrainConclusion
from ...schemas.memory_schema import MemoryItem
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ...utils.text import strip_control_chars
from .ooc_guard import OOCGuard
from .parrot import ParrotGuard


class DeliveryService:
    """说话通道：事前提示 -> 生成（流式/缓冲）-> 出口守门 -> 去重 -> 投递 -> 状态回填。"""

    def __init__(
        self,
        responder: Responder,
        mouth: Mouth,
        character_name: str,
        presence: PresenceTracker,
        energy: EnergyModel | None,
        parrot: ParrotGuard,
        ooc: OOCGuard,
    ) -> None:
        """初始化。

        Args:
            responder: 表达层（respond / respond_stream / stall / correct）
            mouth: 口层（输出投递；supports_streaming 决定流式与否）
            character_name: 角色名（投递时的说话者署名）
            presence: 活跃度统计（发言回填；与门控共用同一个实例）
            energy: 精力模型（None = 不跟踪；低精力注入简短提示）
            parrot: 防复读守门（事前提示 / 出口纠偏 / 收尾去重 / 文风窗口）
            ooc: 出戏守门（blocking 审查 / 硬规则 / 可疑标记）
        """
        self._logger = LoggerManager.get_logger("DELIVERY")
        self._responder = responder
        self._mouth = mouth
        self._character_name = character_name
        self._presence = presence
        self._energy = energy
        self._parrot = parrot
        self._ooc = ooc

    async def deliver(
        self,
        snapshot: SceneSnapshot,
        conclusion: BrainConclusion,
        memories: list[MemoryItem],
        *,
        ooc_guard: bool = True,
    ) -> str:
        """表达 + 投递的统一入口（主循环与主动发言共用）。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论（主动发言为 pass_through 规则结论）
            memories: 检索到的记忆
            ooc_guard: 缓冲路径是否做 OOC 硬规则守门（流式路径恒不做，
                靠后置 System-1 审查兜底——已投递的文本撤不回，重在记录与干预）

        Returns:
            str: 完整回复文本（供记忆落盘 / 后置审查）
        """
        # 防复读预防性提示：两条路径都生效（流式已投递无从改起，只能事前防）
        avoid = self._parrot.avoid_hint()
        # 精力状态提示：低精力时让 Responder 长话短说（空串不注入）
        state_hint = self._energy.verbosity_hint() if self._energy is not None else ""
        # blocking 闸门开启时**放弃流式**：回复必须先完整生成、过 System-1 出戏审查
        # 才放行——逐字蹦的手感换「说出口的话都过了审」（本地模型每回合多一次
        # 审查调用；侧链模式则两不误，默认）
        if self._mouth.supports_streaming and not self._ooc.blocking:
            reply = await self._deliver_stream(
                snapshot, conclusion, memories, avoid=avoid, state_hint=state_hint
            )
            await self._ooc.note_suspicious(reply)
            self._parrot.note_reply(reply)
            return reply
        reply = await self._responder.respond(
            conclusion, snapshot, memories, avoid, state_hint=state_hint
        )
        if self._ooc.blocking:
            reply = await self._ooc.guard_blocking(snapshot, reply)
        if ooc_guard:
            reply = await self._ooc.guard_rules(reply)
            reply = await self._parrot.guard(reply)
        await self._ooc.note_suspicious(reply)
        final = self._parrot.dedup_ending(reply)
        await self._mouth.send(self._character_name, strip_control_chars(final))
        self._presence.record(from_bot=True)
        if self._energy is not None:
            self._energy.note_reply()
        self._parrot.note_reply(reply)
        return final

    async def _deliver_stream(
        self,
        snapshot: SceneSnapshot,
        conclusion: BrainConclusion,
        memories: list[MemoryItem],
        avoid: str = "",
        state_hint: str = "",
    ) -> str:
        """流式投递最终回复：responder.respond_stream → mouth.begin/delta/end。

        逐块把回复文本送到可显示的平台（口层流式），并返回完整回复文本
        （供记忆落盘 / 后置 System-1 审查 / 状态回写）。流式模式下跳过 OOC 预审。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论
            memories: 检索到的记忆
            avoid: 防复读提示（生成前注入；空串不注入）
            state_hint: 精力/状态提示（生成前注入；空串不注入）

        Returns:
            str: 完整回复文本（含情绪润色尾缀）
        """
        await self._mouth.begin(self._character_name)
        parts: list[str] = []
        try:
            async for delta in self._responder.respond_stream(
                conclusion, snapshot, memories, avoid, state_hint=state_hint
            ):
                parts.append(delta)
                if delta:
                    await self._mouth.delta(strip_control_chars(delta))
        except Exception:
            self._logger.exception("流式生成失败（结束投递，回退为已产出文本）")
        finally:
            await self._mouth.end()
        reply = "".join(parts)
        self._presence.record(from_bot=True)
        if self._energy is not None:
            self._energy.note_reply()
        self._logger.info(f"流式投递完成: {len(reply)}字")
        return reply

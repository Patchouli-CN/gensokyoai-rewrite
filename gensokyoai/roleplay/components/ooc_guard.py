"""出戏（OOC）守门 —— 硬规则预筛 / blocking 审查 / 后置深审 / 自愈干预。

三道防线 + 一个闭环（全部状态在本组件，世界只持有它）：

1. **硬规则守门**（`guard_rules`，缓冲路径出口）：零成本快筛，命中才花
   一次纠偏重生成——治「作为一个AI语言模型」这类自曝式话术；
2. **blocking 闸门**（`guard_blocking`）：回复进最终缓冲区先过 System-1
   出戏审查，revise 才纠偏、flag 放行——「说出口的话都过了审」；
3. **后置深审**（`audit`，异步侧链）：System-1 多问概率（state 带诱发
   消息，冷面接梗不被一刀切）或旧单点 audit 降级。只记账不撤回；
4. **闭环干预**：`ooc.rate` 超限 -> `on_spike` 抬推理档位下限；审计恢复
   健康 -> 自愈撤销（不长期烧算力）。

所有故障路径都是**放行**：防御不是裁判，一次误拦比一次漏判更伤 RP 体验。
"""

import re
from collections.abc import Callable

from ...core.brain.gate import Judge
from ...core.brain.ooc_detector import OOCDetector
from ...core.brain.ooc_judge import audit_with_judge
from ...core.config import OOCJudgeSettings
from ...core.health import HealthMonitor
from ...core.memorizer.manager import MemoryManager
from ...core.responder.generator import Responder
from ...schemas.brain_schema import BrainThinkEffort, OOCCheck
from ...schemas.scene_schema import SceneSnapshot
from ...utils.logger import LoggerManager
from ..character import Character
from .effort import EffortGovernor

_SUSPICIOUS_BARE = re.compile(r"^[0-9+\-*/().=\s]+$")
""" 纯数字/符号短回复：疑似被用户消息夹带的指令带跑（也可能是冷面接梗——
    只标记不阻断，定夺交给带上下文的 System-1 出戏审查）"""


class OOCGuard:
    """出戏守门：硬规则 / blocking 审查 / 后置深审 / 出戏率闭环干预。"""

    def __init__(
        self,
        ooc: OOCDetector,
        responder: Responder,
        memory: MemoryManager,
        health: HealthMonitor,
        character: Character,
        persona_brief: str,
        effort: EffortGovernor,
        judge: Judge | None,
        judge_cfg: OOCJudgeSettings,
        *,
        retry: bool = True,
        audit: bool = True,
        generation: Callable[[], int],
    ) -> None:
        """初始化。

        Args:
            ooc: 规则快筛 + 旧单点审计器（pre_filter / audit）
            responder: 表达层；纠偏重生成用 correct()
            memory: 记忆管理器（审查 state 的最近对话）
            health: 健康监控（ooc.rate / ooc.suspicious 指标 + 告警干预）
            character: 角色（出戏计数落在 status.extra）
            persona_brief: 人设摘要（进审查 state）
            effort: 档位治理（OOC 飙升抬下限 / 恢复自愈）
            judge: System-1 审查裁判（None = 回退旧单点 audit）
            judge_cfg: System-1 审查配置（阈值 / 预算 / 模式）
            retry: 最终回复命中 OOC 规则时是否花一次纠偏重生成
            audit: 是否在回复发出后跑异步 OOC 审查（不阻塞热路径）
            generation: 代际令牌读取函数（迟到的审计结论不回写新会话）
        """
        self._logger = LoggerManager.get_logger("OOC")
        self._ooc = ooc
        self._responder = responder
        self._memory = memory
        self._health = health
        self._character = character
        self._persona_brief = persona_brief
        self._effort = effort
        self._judge = judge
        self._judge_cfg = judge_cfg
        self._retry = retry
        self._audit = audit
        self._generation = generation

    # ---------------------------------------------------------------- 属性

    @property
    def blocking(self) -> bool:
        """blocking 闸门是否生效（最终缓冲区审查通过才放行）。

        生效条件三合一：配置了裁判 + System-1 审查 enabled + mode=blocking。
        生效时投递放弃流式（先完整生成、审过再发）。
        """
        return (
            self._judge is not None
            and self._judge_cfg.enabled
            and self._judge_cfg.mode == "blocking"
        )

    @property
    def audit_enabled(self) -> bool:
        """是否跑后置异步深审（blocking 已在出口审过并记账，侧链不重复跑）。"""
        return self._audit

    # ---------------------------------------------------------------- 出口守门

    async def guard_rules(self, reply: str) -> str:
        """最终回复的 OOC 规则守门：零成本快筛，命中才花一次纠偏重生成。

        Args:
            reply: Responder 生成的最终回复

        Returns:
            str: 守门后的回复（纠偏成功返回新文本，否则原样）
        """
        if not (self._retry and reply):
            return reply
        hit = self._ooc.pre_filter(reply)
        if not hit.is_ooc:
            return reply
        self._logger.warning(f"最终回复命中 OOC 规则（{hit.reason}），发起一次纠偏")
        try:
            corrected = await self._responder.correct(reply, hit.reason)
        except Exception:
            self._logger.exception("OOC 纠偏重生成失败，原样输出")
            return reply
        if corrected and not self._ooc.pre_filter(corrected).is_ooc:
            flags = int(self._character.status.extra.get("ooc_flags", 0)) + 1
            self._character.status.update(ooc_flags=flags)
            return corrected
        self._logger.error(f"纠偏后仍命中 OOC，原样输出: {corrected[:60]!r}")
        return reply

    async def guard_blocking(self, snapshot: SceneSnapshot, reply: str) -> str:
        """blocking 闸门：进最终缓冲区的回复先过 System-1 出戏审查，通过才放行。

        判定 revise（双高/unsafe/implausible 低线）时花一次纠偏重写；
        纠偏后仍命中硬规则则保留原句 + 告警（不死循环）。
        flag（模糊带）与原句都直接放行——黄色预警不该有阻断权。

        Args:
            snapshot: 场景快照（诱发消息进审查 state）
            reply: 待放行的回复

        Returns:
            str: 审查（+可能纠偏）后的回复
        """
        if not self.blocking:
            return reply
        judge = self._judge
        assert judge is not None  # blocking 已保证非空（mypy 收窄）
        try:
            recent = await self._memory.recent(5)
            check = await audit_with_judge(
                judge,
                persona=self._persona_brief,
                new_message=snapshot.content,
                reply=reply,
                recent=[m.content for m in reversed(recent)],
                bot_name=self._character.name,
                settings=self._judge_cfg,
            )
        except Exception:
            # 审查自身故障 = 放行（防御不是裁判，不能让一次故障吞掉回合）
            self._logger.exception("blocking 出戏审查失败，放行")
            return reply
        if check.decision != "revise":
            if check.decision == "flag":
                self._logger.info(f"blocking 出戏审查 flag（放行）: {check.reason}")
            await self._record_check(check)
            return reply
        self._logger.warning(
            f"blocking 出戏审查 revise，发起一次纠偏: {check.reason} | {check.answers}"
        )
        try:
            corrected = await self._responder.correct(reply, f"出戏原因: {check.reason}")
        except Exception:
            self._logger.exception("blocking 纠偏重生成失败，保留原句")
            await self._record_check(check)
            return reply
        if not corrected or self._ooc.pre_filter(corrected).is_ooc:
            self._logger.error("纠偏后仍不可用，原样放行")
            await self._record_check(check)
            return reply
        self._logger.info(f"blocking 纠偏完成: {corrected[:60]!r}")
        await self._record_check(check)
        return corrected

    async def note_suspicious(self, reply: str) -> None:
        """零成本启发式：纯数字/符号超短回复 = 疑似被用户消息里夹带的指令带跑
        （20 轮实录的 OOC 注入就是回了个「4」）。

        **只标记不阻断**：这也可能是合法的冷面接梗，自动纠偏会误杀——
        定夺交给带上下文的 System-1 出戏审查（blocking 模式下才可能在出口拦下）。

        Args:
            reply: 已生成的回复文本
        """
        core = reply.strip()
        if not core or len(core) > 10 or not _SUSPICIOUS_BARE.match(core):
            return
        flags = int(self._character.status.extra.get("ooc_suspicious", 0)) + 1
        self._character.status.update(ooc_suspicious=flags)
        await self._health.record_metric("ooc.suspicious", 1.0, unit="次")
        self._logger.warning(f"疑似被注入带跑的短回复（已标记，待 System-1 审查定夺）: {reply!r}")

    # ---------------------------------------------------------------- 后置深审

    async def audit(self, snapshot: SceneSnapshot, reply: str) -> None:
        """后置出戏审查（异步侧链，不阻塞回复）。

        两条路径：
        - **System-1 多问**（judge 可用且 enabled）：多问概率 + 接受规则，
          state 带**诱发消息**——「服从了指令的形式」与「丢了角色的魂」
          分开打分，冷面接梗不再被一刀切判死（20 轮真机实录照出的旧盲区）；
        - 旧单点 audit（无裁判时的降级）：JSON 布尔判定，只看人设+回复。

        两条路径都**不撤回已发出的文本**（流式已投递，撤不回），只回写角色
        状态与健康指标（`ooc.rate` 超阈值时 HealthMonitor 自动抬高推理档位）。

        Args:
            snapshot: 本回合场景快照（取诱发消息进审查 state）
            reply: 已发出的最终回复
        """
        if self._judge is not None and self._judge_cfg.enabled:
            await self._audit_judge(snapshot, reply)
            return
        await self._audit_legacy(reply)

    async def _audit_judge(self, snapshot: SceneSnapshot, reply: str) -> None:
        """System-1 出戏审查侧链：多问概率 -> 三档结论 -> 记账/干预。"""
        judge = self._judge
        if judge is None:
            return
        gen = self._generation()
        try:
            recent = await self._memory.recent(5)
            check = await audit_with_judge(
                judge,
                persona=self._persona_brief,
                new_message=snapshot.content,
                reply=reply,
                recent=[m.content for m in reversed(recent)],
                bot_name=self._character.name,
                settings=self._judge_cfg,
            )
        except Exception:
            self._logger.exception("System-1 出戏审查失败（不影响主链路）")
            return
        if gen != self._generation():
            return
        if check.decision == "revise":
            self._logger.warning(
                f"System-1 出戏审查 revise（已记账，文本不撤回）: {check.reason} | {check.answers}"
            )
        elif check.decision == "flag":
            self._logger.info(f"System-1 出戏审查 flag: {check.reason} | {check.answers}")
        await self._record_check(check)

    async def _audit_legacy(self, reply: str) -> None:
        """旧单点 OOC 深审（无 System-1 裁判时的降级路径；只看人设+回复，无诱发消息）。"""
        gen = self._generation()
        try:
            verdict = await self._ooc.audit(reply, self._character.prompt)
            if gen != self._generation():
                return
            audited = int(self._character.status.extra.get("ooc_audited", 0)) + 1
            hits = int(self._character.status.extra.get("ooc_hits", 0)) + (
                1 if verdict.is_ooc else 0
            )
            self._character.status.update(ooc_audited=audited, ooc_hits=hits)
            await self._health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
            if not verdict.is_ooc:
                # 审计恢复健康 -> 撤销干预抬高的档位下限（自愈，不长期烧算力）
                self._effort.recover()
        except Exception:
            self._logger.exception("OOC 深审失败（不影响主链路）")

    async def _record_check(self, check: OOCCheck) -> None:
        """System-1 审查结论记账（blocking 与侧链共用）：ooc_hits/ooc_flags/ooc.rate +
        档位下限自愈。revise 计入 hits（出戏率），flag 只计 flags 不进率——
        模糊带的判定不该把出戏率推高触发误干预。

        Args:
            check: 审查结论（含三档 decision 与原始概率）
        """
        audited = int(self._character.status.extra.get("ooc_audited", 0)) + 1
        hits = int(self._character.status.extra.get("ooc_hits", 0)) + (
            1 if check.decision == "revise" else 0
        )
        flags = int(self._character.status.extra.get("ooc_flags", 0)) + (
            1 if check.decision == "flag" else 0
        )
        self._character.status.update(ooc_audited=audited, ooc_hits=hits, ooc_flags=flags)
        await self._health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
        if check.decision != "revise":
            # 审查恢复健康 -> 撤销干预抬高的档位下限（自愈，不长期烧算力）
            self._effort.recover()

    # ---------------------------------------------------------------- 闭环干预

    async def on_spike(self, alert) -> None:
        """干预：出戏率飙升 → 抬高档位下限（文档 §3.5「自动调参」）。

        Args:
            alert: 健康告警（取其指标值进日志）
        """
        value = alert.metric.value if alert.metric else 0.0
        self._logger.warning(f"出戏率偏高（{value:.2f}），推理档位下限抬到 HIGH")
        self._effort.raise_floor(BrainThinkEffort.HIGH)

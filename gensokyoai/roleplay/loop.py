""" 角色扮演主循环 """
import asyncio
import contextlib
import random
import time
from ..eyes.perceiver import Perceiver
from ..core.brain.engine import BrainEngine, route
from ..core.brain.ooc_detector import OOCDetector
from ..core.responder.generator import Responder
from ..core.session_manager import SessionManager
from ..core.memorizer.manager import MemoryManager
from ..core.memorizer.compressor import Compressor
from ..core.event_bus import EventBus
from ..core.lifecycle import LifecycleManager
from ..core.registry import ToolRegistry
from ..core.health import HealthMonitor
from ..utils.logger import LoggerManager
from ..utils.text import strip_control_chars
from ..schemas.brain_schema import BrainConclusion, BrainThinkEffort
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import ToolSpec
from ..schemas.scene_schema import SceneSnapshot
from ..schemas.event_schema import BaseEvent, EventTopic
from .character import Character
from .initiative import evaluate_initiative

EXIT_WORDS = {"exit", "quit", "q", "退出"}

_STALL_EFFORTS = {BrainThinkEffort.HIGH, BrainThinkEffort.MAX}
""" 值得垫过渡语的档位：多轮接力思考，延迟肉眼可见 """

class TouhouWorld:
    """
    扮演主循环 (Role-Play Loop)
    负责协调 Eyes, Brain, Responder 和 Memorizer 的完整生命周期。
    支持生命周期管理（启动/关闭回调）、记忆蒸馏、主动发言（对话欲）、
    深思考过渡语与 OOC 守门/后置深审。
    """

    def __init__(
        self,
        eye: Perceiver,
        character: Character,
        sessions: SessionManager,
        external_tools: list[ToolSpec] | None = None,
        *,
        distill_every: int = 10,
        distill_batch: int = 8,
        initiative_interval: float = 30.0,
        idle_threshold: float = 180.0,
        urge_threshold: float = 0.35,
        stall_probability: float = 0.6,
        stall_cooldown_turns: int = 3,
        stall_min_interval: float = 180.0,
        ooc_retry: bool = True,
        ooc_audit: bool = True,
    ) -> None:
        """
        Args:
            eye: 感知器（平台适配）
            character: 角色（人设卡 + 运行时状态）
            sessions: 多模型路由的会话管理器
            external_tools: 外部注入的工具
            distill_every: 每 N 个回合触发一次记忆蒸馏
            distill_batch: 单次蒸馏压缩的记忆条数
            initiative_interval: 主动发言评估周期（秒）
            idle_threshold: 触发主动发言评估的最小空闲（秒）
            urge_threshold: 对话欲阈值，达到才开口
            stall_probability: 深思考前垫过渡语的概率（0 关闭该行为）
            stall_cooldown_turns: 两次过渡语之间的最小回合间隔
            stall_min_interval: 两次过渡语之间的最小时间间隔（秒）
            ooc_retry: 最终回复命中 OOC 规则时是否花一次纠偏重生成
            ooc_audit: 是否在回复发出后跑异步 OOC 深审（不阻塞热路径）
        """
        self.logger = LoggerManager.get_logger("TOUHOU WORLD")
        self.eye = eye
        self.character = character

        # 初始化核心组件
        self.bus = EventBus()
        self.lifecycle = LifecycleManager(self.bus)
        self.sessions = sessions
        self.memory = MemoryManager()
        self.health = HealthMonitor(bus=self.bus)
        self.compressor = Compressor(sessions)

        # --- 组装工具：注册表（内置） + 外部传入 ---
        all_tools = self._setup_tools(external_tools)

        # 初始化业务模块
        self.ooc = OOCDetector(self.sessions)
        self.brain = BrainEngine(
            sessions=self.sessions,
            persona=character.prompt,
            ooc=self.ooc,
            tools=all_tools
        )

        self.responder = Responder(sessions=self.sessions, persona=character.prompt)

        # --- 主动发言 / 蒸馏旋钮 ---
        self._distill_every = distill_every
        self._distill_batch = distill_batch
        self._initiative_interval = initiative_interval
        self._idle_threshold = idle_threshold
        self._urge_threshold = urge_threshold

        # --- 过渡语 / OOC 旋钮 ---
        self._stall_probability = stall_probability
        self._stall_cooldown_turns = stall_cooldown_turns
        self._stall_min_interval = stall_min_interval
        self._ooc_retry = ooc_retry
        self._ooc_audit = ooc_audit

        # --- 运行时状态 ---
        self._generation = 0
        """ 代际令牌：后台任务（蒸馏/主动发言）持任务发起时的代际，
        回写前校验，防止关闭/重置后的迟到写入污染新会话 """
        self._last_activity = time.monotonic()
        self._distill_counter = 0
        self._busy = False
        """ 主链路生成中标志：避免主动发言与用户回复并发抢同一个 responder 会话 """
        self._stall_last_turn = -10**9
        """ 上次垫过渡语的回合号（冷却门控用）"""
        self._stall_last_time = float("-inf")
        """ 上次垫过渡语的时刻（冷却门控用）"""

        # 注册记忆写入侧链
        self.bus.subscribe(EventTopic.MEMORY_WRITE, self._handle_memory_write)

        # 注册默认生命周期回调
        self._register_default_lifecycle()

    def _register_default_lifecycle(self) -> None:
        """ 注册默认的生命周期回调 """

        @self.lifecycle.on_startup
        async def _on_startup():
            self.logger.info(f"=== 幻想乡连接成功 | 角色: {self.character.name} ===")

        @self.lifecycle.on_shutdown
        async def _on_shutdown():
            self.logger.info("=== 幻想乡连接已断开 ===")
            await self.eye.close()
            self.sessions.reset_all()

    def _setup_tools(self, external_tools: list[ToolSpec] | None = None) -> list[ToolSpec]:
        """ 组装工具列表 """
        tools = []

        try:
            registered_tools = ToolRegistry.all()
            if registered_tools:
                tools.extend(registered_tools)
                self.logger.info(f"加载全局注册工具: {[t.tool_func.__name__ for t in registered_tools]}")
        except Exception as e:
            self.logger.warning(f"加载全局注册工具失败: {e}")

        if external_tools:
            for tool in external_tools:
                tools.append(tool)
                try:
                    ToolRegistry.register_external(tool)
                except ValueError:
                    self.logger.debug(f"工具已存在: {tool.tool_func.__name__}")
            self.logger.info(f"加载外部工具: {[t.tool_func.__name__ for t in external_tools]}")

        seen = set()
        unique_tools = []
        for tool in tools:
            name = tool.tool_func.__name__
            if name not in seen:
                unique_tools.append(tool)
                seen.add(name)

        self.logger.info(f"最终工具列表: {[t.tool_func.__name__ for t in unique_tools]}")
        return unique_tools

    async def _handle_memory_write(self, event: BaseEvent) -> None:
        """ 异步处理记忆存储，不阻塞主链路 """
        if isinstance(event.payload, MemoryItem):
            await self.memory.store(event.payload)

    async def start(self) -> None:
        """ 启动主循环（开场白 + 主动发言后台任务 + 优雅关闭）"""
        await self.lifecycle.startup()
        self._greet()
        initiative_task = asyncio.create_task(self._initiative_loop())

        turn = 0
        try:
            while True:
                # 检查是否收到停止请求
                if getattr(self.eye, '_stop_requested', False):
                    self.logger.info("感知器已停止，主循环退出")
                    break

                # 1. 感知阶段 (Eyes)
                snapshot = await self.eye.next_snapshot()
                if snapshot is None:
                    # 如果是因为停止请求返回 None，直接退出
                    if getattr(self.eye, '_stop_requested', False):
                        break
                    continue

                # 退出信号检查
                if snapshot.content.lower() in EXIT_WORDS:
                    break

                turn += 1
                t_start = time.monotonic()
                self._last_activity = time.monotonic()
                self._busy = True
                try:
                    # 2. 决策阶段 (Brain)；深思考前先垫一句角色过渡语遮延迟
                    memories = await self.memory.recent(5)
                    effort = route(snapshot)
                    await self._maybe_stall(snapshot, effort, turn)
                    conclusion = await self.brain.think(snapshot, memories, effort)

                    # 3. 表达阶段 (Responder)；OOC 规则守门，命中才纠偏重生成
                    reply = await self.responder.respond(conclusion, snapshot, memories)
                    reply = await self._guard_ooc(reply)
                finally:
                    self._busy = False

                # 4. 输出与记录（投递档清洗控制字符；记忆档保留原文）
                print(f"\n{self.character.name}: {strip_control_chars(reply)}\n")
                self._remember_turn(snapshot, reply)
                if self._ooc_audit:
                    asyncio.create_task(self._audit_reply(reply))

                # 5. 角色状态跟踪 + 健康喂食
                if conclusion.emotion:
                    self.character.status.update(emotion=conclusion.emotion)
                await self._record_health(turn, effort, t_start)

                # 6. 记忆蒸馏（后台侧链，不阻塞回合）
                self._distill_counter += 1
                if self._distill_counter >= self._distill_every:
                    self._distill_counter = 0
                    asyncio.create_task(self._distill())

                latency = time.monotonic() - t_start
                self.logger.info(f"回合 {turn} 完成 | 延迟: {latency:.2f}s | 档位: {effort.value}")

        except asyncio.CancelledError:
            self.logger.info("主循环被取消，开始优雅关闭...")
        except KeyboardInterrupt:
            self.logger.info("收到 Ctrl+C，开始优雅关闭...")
        finally:
            # 代际 +1：在途后台任务的回写全部作废
            self._generation += 1
            initiative_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await initiative_task
            if not self.lifecycle._stopped:
                await self.lifecycle.shutdown()

    def _greet(self) -> None:
        """ 打出角色开场白（若有）"""
        greeting = self.character.card.greeting
        if greeting:
            print(f"\n{self.character.name}: {greeting}\n")

    def _remember_turn(self, snapshot: SceneSnapshot, reply: str) -> None:
        """ 把本回合的用户输入与角色回复投递到记忆总线（异步侧链）"""
        user_mem = MemoryItem(
            topic="对话", content=f"{snapshot.sender}: {snapshot.content}", memory_type="dialogue"
        )
        char_mem = MemoryItem(
            topic="对话", content=f"{self.character.name}: {reply}", memory_type="dialogue",
            relate_ids={user_mem.memory_id}
        )
        for item in [user_mem, char_mem]:
            asyncio.get_running_loop().create_task(
                self.bus.publish(EventBus.new(EventTopic.MEMORY_WRITE, source="loop", payload=item))
            )

    async def _record_health(self, turn: int, effort, t_start: float) -> None:
        """ 回合粒度的健康指标喂食 """
        await self.health.record(
            {
                "turn.count": 1,
                f"effort.{effort.value}": 1,
                "memory.work_size": self.memory.work_mem_size,
                "memory.long_size": self.memory.long_mem_size,
            }
        )
        await self.health.record_metric("turn.latency_s", time.monotonic() - t_start, unit="s")

    def _should_stall(self, effort: BrainThinkEffort, turn: int) -> bool:
        """ 是否值得垫过渡语：仅深思考档、非开局、出了冷却期、再掷中概率。

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
        if turn - self._stall_last_turn < self._stall_cooldown_turns:
            return False
        if time.monotonic() - self._stall_last_time < self._stall_min_interval:
            return False
        return random.random() < self._stall_probability

    async def _maybe_stall(self, snapshot: SceneSnapshot, effort: BrainThinkEffort, turn: int) -> None:
        """ 深思考前垫一句角色口吻过渡语（如"唔……让我想想"），掩盖接力思考延迟。

        三重门控防止人机感：冷却轮数 + 时间间隔 + 概率掷骰。
        过渡语只投递显示层，不写记忆（对 Brain 是噪音）；
        但它进了 responder 有状态会话，正式回复能看到它、自然承接不重复。
        """
        if not self._should_stall(effort, turn):
            return
        try:
            line = await self.responder.stall(snapshot)
        except Exception:
            self.logger.exception("过渡语生成失败（跳过，不影响主链路）")
            return
        if not line:
            return
        self._stall_last_turn = turn
        self._stall_last_time = time.monotonic()
        print(f"\n{self.character.name}: {strip_control_chars(line)}\n")
        self.logger.info(f"过渡语已投递: {line!r}")

    async def _guard_ooc(self, reply: str) -> str:
        """ 最终回复的 OOC 规则守门：零成本快筛，命中才花一次纠偏重生成。

        Args:
            reply: Responder 生成的最终回复

        Returns:
            str: 守门后的回复（纠偏成功返回新文本，否则原样）
        """
        if not (self._ooc_retry and reply):
            return reply
        hit = self.ooc.pre_filter(reply)
        if not hit.is_ooc:
            return reply
        self.logger.warning(f"最终回复命中 OOC 规则（{hit.reason}），发起一次纠偏")
        try:
            corrected = await self.responder.correct(reply, hit.reason)
        except Exception:
            self.logger.exception("OOC 纠偏重生成失败，原样输出")
            return reply
        if corrected and not self.ooc.pre_filter(corrected).is_ooc:
            flags = int(self.character.status.extra.get("ooc_flags", 0)) + 1
            self.character.status.update(ooc_flags=flags)
            return corrected
        self.logger.error(f"纠偏后仍命中 OOC，原样输出: {corrected[:60]!r}")
        return reply

    async def _audit_reply(self, reply: str) -> None:
        """ 后置 OOC 深审（异步侧链，不阻塞回复）。

        审计结论不撤回已发出的文本，只回写角色状态与 ooc.rate 健康指标
        （滚动出戏率超过阈值 0.5 时 HealthMonitor 自动告警）。

        Args:
            reply: 已发出的最终回复
        """
        gen = self._generation
        try:
            verdict = await self.ooc.audit(reply, self.character.prompt)
            if gen != self._generation:
                return
            audited = int(self.character.status.extra.get("ooc_audited", 0)) + 1
            hits = int(self.character.status.extra.get("ooc_hits", 0)) + (1 if verdict.is_ooc else 0)
            self.character.status.update(ooc_audited=audited, ooc_hits=hits)
            await self.health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
        except Exception:
            self.logger.exception("OOC 深审失败（不影响主链路）")

    async def _distill(self) -> None:
        """ 记忆蒸馏：把最早一批工作记忆压缩成摘要条目，然后遗忘原文。

        摘要 importance=0.7，入库即自动落长期记忆；代际令牌校验防止
        关闭后的迟到写入。
        """
        gen = self._generation
        try:
            old = self.memory.oldest(self._distill_batch)
            if not old:
                return
            summary = await self.compressor.compress(old)
            if not summary or gen != self._generation:
                return
            item = MemoryItem(
                topic="对话摘要", content=summary, memory_type="fact", importance=0.7,
            )
            await self.memory.store(item)
            removed = self.memory.forget([m.memory_id for m in old])
            self.logger.info(f"记忆蒸馏: {removed} 条 -> 摘要 {len(summary)} 字")
        except Exception:
            self.logger.exception("记忆蒸馏失败（不影响主链路）")

    async def _initiative_loop(self) -> None:
        """ 主动发言后台循环：空闲超阈值时评估四维对话欲，达标即开口。

        零思考 token：不经过 Brain，直接用规则结论驱动 Responder。
        """
        while True:
            await asyncio.sleep(self._initiative_interval)
            if self._busy or self._stopping():
                continue
            idle = time.monotonic() - self._last_activity
            if idle < self._idle_threshold:
                continue

            try:
                await self._try_speak(idle)
            except Exception:
                self.logger.exception("主动发言评估失败")

    def _stopping(self) -> bool:
        """ 是否处于关闭流程 """
        return getattr(self.eye, '_stop_requested', False)

    async def _try_speak(self, idle: float) -> None:
        """ 评估对话欲并尝试主动开口 """
        gen = self._generation
        recent = await self.memory.recent(6)
        urge = evaluate_initiative(
            [m.content for m in reversed(recent)],
            character_name=self.character.name,
            weights=self.character.card.motivation_weights,
            idle_seconds=idle,
        )
        self.character.status.update(motivation=round(urge, 2))
        if urge < self._urge_threshold:
            return
        if self._busy or gen != self._generation:
            return

        self.logger.info(f"主动发言触发: 对话欲={urge:.2f} 空闲={idle:.0f}s")
        self._busy = True
        try:
            snapshot = SceneSnapshot(
                scene_type="group_chat",
                sender="环境",
                content="（周围安静下来了）",
                is_direct=False,
                context_snippet=[m.content for m in reversed(recent)][-5:],
                timestamp=time.time(),
            )
            conclusion = BrainConclusion(
                verdict="pass_through",
                intent="主动发起话题",
                emotion=self.character.status.emotion,
            )
            reply = await self.responder.respond(conclusion, snapshot, recent)
        finally:
            self._busy = False

        print(f"\n{self.character.name}: {strip_control_chars(reply)}\n")
        char_mem = MemoryItem(
            topic="对话", content=f"{self.character.name}: {reply}", memory_type="dialogue",
        )
        await self.bus.publish(
            EventBus.new(EventTopic.MEMORY_WRITE, source="initiative", payload=char_mem)
        )
        self._last_activity = time.monotonic()

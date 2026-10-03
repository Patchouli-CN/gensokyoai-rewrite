"""角色扮演主循环"""

import asyncio
import time
from pathlib import Path

from ..core.brain.energy import EnergyModel
from ..core.brain.engine import BrainEngine, build_tool_directive, route
from ..core.brain.gate import Judge, PresenceTracker, tier_from_deep_score
from ..core.brain.ooc_detector import OOCDetector
from ..core.brain.pipeline import ThinkPipeline
from ..core.clock import BiologicalClock
from ..core.config import (
    EnergySettings,
    GateSettings,
    OOCJudgeSettings,
    SearchSettings,
    StyleSettings,
    WorldSettings,
)
from ..core.event_bus import EventBus
from ..core.health import HealthMonitor
from ..core.lifecycle import LifecycleManager
from ..core.memorizer.compressor import Compressor
from ..core.memorizer.embedder import Embedder
from ..core.memorizer.knowledge import KnowledgeCache
from ..core.memorizer.manager import MemoryManager
from ..core.persistence import JsonFilePersistence, PersistenceBackend
from ..core.registry import ToolRegistry
from ..core.responder.generator import Responder
from ..core.session_manager import SessionManager
from ..mouth.base import Mouth
from ..mouth.console import ConsoleMouth
from ..satori.perceiver import Perceiver
from ..satori.queue import merge_snapshots
from ..schemas.brain_schema import BrainThinkEffort
from ..schemas.event_schema import BaseEvent, EventTopic, TurnEndPayload
from ..schemas.memory_schema import MemoryItem, MemoryType
from ..schemas.model_schema import ToolSpec
from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager
from ..utils.tasks import TaskManager
from .character import Character
from .components.delivery import DeliveryService
from .components.effort import EffortGovernor
from .components.gate_ctl import System1Gate
from .components.health_report import HealthReporter
from .components.initiative_speaker import InitiativeSpeaker
from .components.ooc_guard import OOCGuard
from .components.parrot import ParrotGuard
from .components.stall import StallSpeaker
from .persistence import CharacterStateCodec, SessionPersister
from .runtime import RuntimeState, WorldRuntimeCodec
from .trace import ReasoningTrace

EXIT_WORDS = {"exit", "quit", "q", "退出"}


class TouhouWorld:
    """
    扮演主循环 (Role-Play Loop)
    负责协调 Eyes, Brain, Responder 和 Memorizer 的完整生命周期。
    支持生命周期管理（启动/关闭回调）、记忆蒸馏、主动发言（对话欲）、
    深思考过渡语与 OOC 守门/后置深审、System-1 发言门控（见 core/brain/gate.py）。
    """

    def __init__(
        self,
        eye: Perceiver,
        character: Character,
        sessions: SessionManager,
        mouth: Mouth | None = None,
        external_tools: list[ToolSpec] | None = None,
        judge: Judge | None = None,
        gate: GateSettings | None = None,
        *,
        settings: WorldSettings | None = None,
        ooc_judge: Judge | None = None,
        ooc_judge_settings: OOCJudgeSettings | None = None,
        style: StyleSettings | None = None,
        energy: EnergySettings | None = None,
        search: SearchSettings | None = None,
        session_id: str = "default",
        storage_dir: str | Path = "data",
        persistence=None,
        embedder: Embedder | None = None,
        memory_min_score: float = 0.0,
    ) -> None:
        """
        Args:
            eye: 感知器（平台适配）
            character: 角色（人设卡 + 运行时状态）
            sessions: 多模型路由的会话管理器
            external_tools: 外部注入的工具
            judge: 发言门控裁判（None = 无裁判，门控走规则+兜底；见 core/brain/gate.py）
            gate: 门控配置（None = GateSettings() 默认；enabled=False 维持「每条都回」旧行为）
            distill_every: 每 N 个回合触发一次记忆蒸馏
            distill_batch: 单次蒸馏压缩的记忆条数
            initiative_interval: 主动发言评估周期（秒）
            idle_threshold: 触发主动发言评估的最小空闲（秒）
            urge_threshold: 对话欲阈值，达到才开口
            stall_probability: 深思考前垫过渡语的概率（0 关闭该行为）
            stall_cooldown_turns: 两次过渡语之间的最小回合间隔
            stall_min_interval: 两次过渡语之间的最小时间间隔（秒）
            ooc_retry: 最终回复命中 OOC 规则时是否花一次纠偏重生成
            ooc_audit: 是否在回复发出后跑异步 OOC 审查（不阻塞热路径）
            ooc_judge: 出戏审查裁判（System-1 多问概率；None 回退旧单点 audit）
            ooc_judge_settings: System-1 出戏审查配置（阈值/预算；None = 默认）
            style: 文风防复读配置（None = StyleSettings() 默认）
            energy: 精力模型配置（None = EnergySettings() 默认；enabled=False 不跟踪精力）
            search: 联网工具配置（可信知识站表 -> 脑内工具指令；None = 无指令）
            session_id: 会话标识，记忆与会话快照按它隔离（多群/多用户各自一个 id）
            storage_dir: 持久化根目录
            persistence: 可插拔持久化后端；None 用默认 JsonFilePersistence(storage_dir)
            embedder: 记忆向量化器（None = 长期记忆检索退回子串匹配）
            memory_min_score: 语义检索相似度下限（0 = 不过滤；config.embedding.min_score 传入）
            settings: 世界行为旋钮（WorldSettings：蒸馏节奏 / 主动发言 / 过渡语 /
                OOC / 工具 / 关闭；None = 全默认）
        """
        s = settings or WorldSettings()
        self._logger = LoggerManager.get_logger("WORLD")
        self.eye = eye
        self.character = character
        self.mouth = mouth or ConsoleMouth()
        """ 口层：输出投递（默认 ConsoleMouth）"""

        # 初始化核心组件
        self.bus = EventBus()
        self.lifecycle = LifecycleManager(self.bus)
        self.sessions = sessions
        self.session_id = session_id
        """ 会话标识：记忆与快照按它隔离 """
        self._tasks = TaskManager("WORLD")
        """ 后台侧链（记忆投递 / 蒸馏 / OOC 审计）的任务管理器：
        `asyncio` 只对任务持弱引用，不登记就可能执行途中被 GC 回收；
        关闭时也靠它统一取消并等在途任务收尾 """
        self._runtime = RuntimeState()
        """ 跨回合运行时状态（busy / generation / last_activity；组件与主循环注入同一实例）"""
        self.memory = MemoryManager(
            storage_dir=storage_dir,
            session_id=session_id,
            tasks=self._tasks,
            embedder=embedder,
            min_score=memory_min_score,
        )
        self._knowledge = KnowledgeCache(
            self.memory, ttl_s=(search or SearchSettings()).cache_ttl_s
        )
        """ 联网工具知识缓存（L1 会话内 + L2 落本世界长期记忆） """
        self.health = HealthMonitor(
            bus=self.bus,
            session_provider=self._session_health,
        )
        """ 健康监控 + 主动干预（见 §3.5）"""
        self._health_report = HealthReporter(sessions, self.memory, self.health)
        """ 回合健康指标喂食（token/费用差值快照的安家；见 components/health_report.py）"""

        self._effort = EffortGovernor()
        """ 推理档位治理：注入识别 / OOC 干预的抬升与自愈（见 components/effort.py）"""

        self.compressor = Compressor(sessions)

        # --- 组装工具：注册表（内置） + 外部传入 ---
        all_tools = self._setup_tools(external_tools)

        # --- 人设摘要：给「想方向」的环节（门控 / 思考链步骤与结论）用，压 token；
        # 完整人设只留给开口的 Responder（那里一个字都省不得）---
        self._persona_brief = f"【{character.name}】{character.card.system_prompt[:200]}"

        # 初始化业务模块
        self.ooc = OOCDetector(self.sessions)
        self.brain = BrainEngine(
            sessions=self.sessions,
            persona=character.prompt,
            ooc=self.ooc,
            tools=all_tools,
            tool_timeout=s.tool_timeout,
            tool_max_result_chars=s.tool_max_result_chars,
            pipeline=self._build_think_pipeline(),
            persona_brief=self._persona_brief,
            tool_directive=build_tool_directive((search or SearchSettings()).knowledge_sites),
        )

        self.responder = Responder(sessions=self.sessions, persona=character.prompt)

        # --- 发言门控（System-1 混合门控；enabled=False 时维持「每条都回」旧行为）---
        self._gate = gate if gate is not None else GateSettings()
        self._judge = judge
        self._presence = PresenceTracker(window_s=self._gate.presence_window_s)
        """ 活跃度统计：喂给裁判的 bot_activity（防刷屏靠裁判自觉，不设硬冷却） """
        self._energy_cfg = energy or EnergySettings()
        self._energy = (
            EnergyModel(self._energy_cfg, self._presence) if self._energy_cfg.enabled else None
        )
        """ 精力模型（HumanLikeSystem 阶段二）：与门控共用 PresenceTracker，
        调制群聊模糊带阈值与回复长度；None = 不跟踪（旧行为） """
        self._gate_ctl = System1Gate(
            self._gate,
            self._judge,
            self._presence,
            self._energy,
            self._persona_brief,
            character.name,
            self.memory,
        )
        """ System-1 发言门控（该不该接话 + 该想多深；纯判决见 core/brain/gate.py）"""

        # --- 蒸馏 / 主动发言旋钮 ---
        self._distill_every = s.distill_every
        self._distill_batch = s.distill_batch
        self._initiative_interval = s.initiative_interval

        self._ooc_guard = OOCGuard(
            self.ooc,
            self.responder,
            self.memory,
            self.health,
            character,
            self._persona_brief,
            self._effort,
            ooc_judge,
            ooc_judge_settings or OOCJudgeSettings(),
            retry=s.ooc_retry,
            audit=s.ooc_audit,
            generation=lambda: self._runtime.generation,
        )
        """ 出戏守门：硬规则 / blocking 审查 / 后置深审 / 闭环干预（见 components/ooc_guard.py）"""

        # --- 健康干预：把「监控」接成「动作」（装配层注册，core/health 不反向依赖）---
        # ooc.rate 的干预住在 OOCGuard 里（on_spike），组件造好后才能注册
        self.health.register_intervention("session.context_usage", self._on_context_pressure)
        self.health.register_intervention("ooc.rate", self._ooc_guard.on_spike)

        self._style = style or StyleSettings()
        """ 文风防复读配置 """
        self._parrot = ParrotGuard(self._style, self.responder)
        """ 防复读守门（文风窗口状态的安家）"""
        self._stall = StallSpeaker(
            self.responder,
            self.mouth,
            character.name,
            probability=s.stall_probability,
            cooldown_turns=s.stall_cooldown_turns,
            min_interval=s.stall_min_interval,
        )
        """ 深思考过渡语（延迟掩盖 + 三重门控；见 components/stall.py）"""
        self._delivery = DeliveryService(
            self.responder,
            self.mouth,
            character.name,
            self._presence,
            self._energy,
            self._parrot,
            self._ooc_guard,
        )
        """ 表达 + 投递管线（主循环与主动发言共用的「说话」通道；见 components/delivery.py）"""
        self._initiative = InitiativeSpeaker(
            character,
            self.memory,
            self.bus,
            self._runtime,
            self._delivery,
            urge_threshold=s.urge_threshold,
            idle_threshold=s.idle_threshold,
            is_stopping=lambda: self.eye.stopping,
        )
        """ 主动发言节拍（冷场时角色自己冒泡；见 components/initiative_speaker.py）"""

        # --- 生物钟：定时任务调度（首个住户 = 主动发言节拍）---
        self.clock = BiologicalClock()
        self.clock.every("initiative", self._initiative_interval, self._initiative.tick)

        self._shutdown_drain_timeout = s.shutdown_drain_timeout
        """ 关闭时等后台侧链收尾的秒数 """

        # 注册记忆写入侧链
        self.bus.subscribe(EventTopic.MEMORY_WRITE, self._handle_memory_write)

        # --- 会话持久化：可插拔后端，事件驱动落盘 ---
        # 必须在存储订阅之后创建：MEMORY_WRITE 处理器按订阅顺序执行，
        # persister 的快照要看到本事件刚写入的记忆
        persistence_backend: PersistenceBackend = persistence or JsonFilePersistence(storage_dir)
        self.persistence = SessionPersister(
            persistence_backend,
            session_id,
            self.memory,
            sessions,
            self.bus,
            character_name=character.name,
            codecs=[
                CharacterStateCodec(character),
                WorldRuntimeCodec(self._runtime, self._effort, self._stall),
            ],
        )
        """ 会话持久化器（启动 restore / 事件驱动保存 / 关闭 flush）"""

        self.trace = ReasoningTrace(storage_dir, session_id, enabled=s.trace_steps)
        """ 思考轨迹留档（每回合一行 JSONL，含逐轮 ReasoningStep）"""

        # 注册默认生命周期回调
        self._register_default_lifecycle()

        self.restored = False
        """ 启动时是否成功恢复了历史会话 """

    @property
    def busy(self) -> bool:
        """主链路是否正在生成（供外部观察，如真机验证脚本探活）。

        Returns:
            bool: True 表示主循环正在本回合内生成
        """
        return self._runtime.busy

    def _register_default_lifecycle(self) -> None:
        """注册默认的生命周期回调"""

        @self.lifecycle.on_startup
        async def _on_startup():
            self.restored = await self.persistence.restore()
            if self.restored:
                self._logger.info(f"=== 幻想乡会话已恢复 | 角色: {self.character.name} ===")
            else:
                self._logger.info(f"=== 幻想乡连接成功 | 角色: {self.character.name} ===")
            if self._gate.enabled:
                judge_name = type(self._judge).__name__ if self._judge else "无（纯规则）"
                self._logger.info(
                    f"门控 on | 裁判: {judge_name} | 群聊阈值: {self._gate.group_threshold}"
                )

        @self.lifecycle.on_shutdown
        async def _on_shutdown():
            self._logger.info("=== 幻想乡连接已断开 ===")
            await self.persistence.flush()
            await self.eye.close()
            self.sessions.reset_all()

    def _build_think_pipeline(self) -> ThinkPipeline | None:
        """按角色卡 think_chain 拼定制思考链；空链 = 用内置接力思考。

        支持混合项：内置步骤名（字符串） / 内联自定义步骤（字典，字段同
        ThinkStep）——后者让角色卡作者直接写「这一步想什么」，无需改代码。
        链的**执行**在 BrainEngine.think() 里内联 await（串行步骤、每步
        wait_for 超时、CancelledError 穿透），本方法只做同步拼装，
        未注册的步骤名 / 非法步骤字典会在启动时直接抛错（配置错误早暴露）。
        """
        items = self.character.card.think_chain
        if not items:
            return None
        pipeline = ThinkPipeline.from_card(f"{self.character.name}·思考链", items)
        self._logger.info(
            f"思考链({len(pipeline)}步): {' >> '.join(step.name for step in pipeline)}"
        )
        return pipeline

    def _setup_tools(self, external_tools: list[ToolSpec] | None = None) -> list[ToolSpec]:
        """组装工具列表"""
        tools = []

        try:
            registered_tools = ToolRegistry.all()
            if registered_tools:
                tools.extend(registered_tools)
                self._logger.info(
                    f"加载全局注册工具: {[t.tool_func.__name__ for t in registered_tools]}"
                )
        except Exception as err:
            self._logger.warning(f"加载全局注册工具失败: {err}")

        if external_tools:
            for tool in external_tools:
                tools.append(tool)
                try:
                    ToolRegistry.register_external(tool)
                except ValueError:
                    self._logger.debug(f"工具已存在: {tool.tool_func.__name__}")
            self._logger.info(f"加载外部工具: {[t.tool_func.__name__ for t in external_tools]}")

        seen = set()
        unique_tools = []
        for tool in tools:
            name = tool.tool_func.__name__
            if name not in seen:
                unique_tools.append(tool)
                seen.add(name)

        # 联网工具套知识缓存（L1 TTL + L2 落长期记忆；对工具链路透明）
        unique_tools = self._knowledge.wrap_all(unique_tools)

        self._logger.info(f"最终工具列表: {[t.tool_func.__name__ for t in unique_tools]}")
        return unique_tools

    async def _handle_memory_write(self, event: BaseEvent) -> None:
        """异步处理记忆存储，不阻塞主链路"""
        if isinstance(event.payload, MemoryItem):
            await self.memory.store(event.payload)

    async def start(self) -> None:
        """启动主循环（开场白 + 生物钟 + 优雅关闭）"""
        await self.lifecycle.startup()
        if not self.restored:
            await self._greet()
        await self.clock.start()

        turn = self.persistence.turn_count
        try:
            while True:
                # 检查是否收到停止请求
                if self.eye.stopping:
                    self._logger.info("感知器已停止，主循环退出")
                    break

                # 1. 感知阶段 (Eyes)
                snapshot = await self.eye.next_snapshot()
                if snapshot is None:
                    # 如果是因为停止请求返回 None，直接退出
                    if self.eye.stopping:
                        break
                    continue

                # 退出信号检查
                if snapshot.content.lower() in EXIT_WORDS:
                    break

                # 忙时攒批：回合期间积压的输入一次性合并进本回合——多人同时 @ /
                # 复读队形只开一回合、过一次门控、回一条（场景无关的合流语义；
                # 无积压时 drain 返回空，零开销）
                backlog = self.eye.drain()
                if backlog:
                    snapshot = merge_snapshots([snapshot, *backlog])

                turn += 1
                t_start = time.monotonic()
                self._runtime.note_activity()
                self._presence.record(from_bot=False)
                with self._runtime.begin_busy():
                    # 2. System-1 层：一次裁判调用回答「该不该发言」+「该想多深」
                    proceed, judged = await self._system1_turn(snapshot, turn)
                    if not proceed:
                        continue

                    # 3. 决策阶段 (Brain)；深思考前先垫一句角色过渡语遮延迟
                    # 以当前消息为关联词，顺带把长期记忆里的相关旧事捞进来（语义检索）
                    memories = await self.memory.recent(5, search_term=snapshot.content)
                    effort = self._effort.floor(judged if judged is not None else route(snapshot))
                    effort = self._effort.floor_for_injection(snapshot, effort)
                    await self._stall.maybe_stall(snapshot, effort, turn)
                    conclusion = await self.brain.think(snapshot, memories, effort)

                    # 4. 表达 + 投递：口层支持流式则逐块显示，否则缓冲投递（流式下跳过 OOC 预审）
                    reply = await self._delivery.deliver(snapshot, conclusion, memories)

                # 5. 记录（投递已在 _delivery 内完成）
                self._remember_turn(snapshot, reply)
                if self.trace.enabled:
                    # 一次小追加（走线程池），保证本回合轨迹已落盘、便于复盘与测试
                    await self.trace.record(
                        turn=turn, effort=effort.value, conclusion=conclusion, reply=reply
                    )
                if self._ooc_guard.audit_enabled and not self._ooc_guard.blocking:
                    # 传 snapshot：System-1 审查需要诱发消息当上下文（旧 audit 只用 reply）。
                    # blocking 模式已在 _delivery 里审过并记账，侧链不重复跑
                    self._tasks.spawn(self._ooc_guard.audit(snapshot, reply), name="ooc-audit")

                # 6. 角色状态跟踪 + 健康喂食
                if conclusion.emotion:
                    self.character.status.update(emotion=conclusion.emotion)
                turn_cost = await self._health_report.record(effort, t_start)

                # 7. 记忆蒸馏（后台侧链，不阻塞回合）
                self._runtime.distill_counter += 1
                if self._runtime.distill_counter >= self._distill_every:
                    self._runtime.distill_counter = 0
                    self._tasks.spawn(self._distill(), name="distill")

                latency = time.monotonic() - t_start
                cost_text = (
                    " | 花费: " + ", ".join(f"{c} {a:.6f}" for c, a in turn_cost.items())
                    if turn_cost
                    else ""
                )
                self._logger.info(
                    f"回合 {turn} 完成 | 延迟: {latency:.2f}s | 档位: {effort.value}{cost_text}"
                )
                await self.bus.publish(
                    EventBus.new(
                        EventTopic.TURN_END,
                        source="loop",
                        payload=TurnEndPayload(turn=turn, effort=effort.value, latency_s=latency),
                    )
                )

        except asyncio.CancelledError:
            self._logger.info("主循环被取消，开始优雅关闭...")
        except KeyboardInterrupt:
            self._logger.info("收到 Ctrl+C，开始优雅关闭...")
        finally:
            # 代际 +1：在途后台任务的回写全部作废
            self._runtime.bump_generation()
            await self.clock.stop()
            # 侧链收尾：先给一小段自然完成的机会（本回合的记忆写入应落盘），
            # 超时则取消 —— 不能让慢蒸馏把关闭流程拖住
            if self._tasks.pending:
                self._logger.info(f"等待后台侧链收尾: {self._tasks.names()}")
                await self._tasks.drain(timeout=self._shutdown_drain_timeout)
            if not self.lifecycle._stopped:
                await self.lifecycle.shutdown()

    async def _greet(self) -> None:
        """打出角色开场白（若有）"""
        greeting = self.character.card.greeting
        if greeting:
            await self.mouth.send(self.character.name, greeting)

    def _remember_turn(self, snapshot: SceneSnapshot, reply: str) -> None:
        """把本回合的用户输入与角色回复投递到记忆总线（异步侧链）。

        投递任务必须登记：这些任务同时驱动「记忆写入」与「持久化置脏」，
        被 GC 回收等于这一回合的记忆凭空消失，且不会有任何报错。
        """
        user_mem = MemoryItem(
            topic="对话",
            content=f"{snapshot.sender}: {snapshot.content}",
            memory_type=MemoryType.DIALOGUE,
        )
        char_mem = MemoryItem(
            topic="对话",
            content=f"{self.character.name}: {reply}",
            memory_type=MemoryType.DIALOGUE,
            relate_ids={user_mem.memory_id},
        )
        for item in [user_mem, char_mem]:
            self._tasks.spawn(
                self.bus.publish(
                    EventBus.new(EventTopic.MEMORY_WRITE, source="loop", payload=item)
                ),
                name="memory-write",
            )

    async def _system1_turn(
        self, snapshot: SceneSnapshot, turn: int
    ) -> tuple[bool, BrainThinkEffort | None]:
        """System-1 回合决策：一次裁判调用回答「该不该发言」与「该想多深」。

        门控开启且裁判说「不接」时：只把用户消息写进记忆（不回复≠没听过），
        并喂 `gate.skip` 健康计数。裁判不可用时整层静默跳过（回退旧行为）。

        Args:
            snapshot: 待判断快照
            turn: 当前回合号（仅日志用）

        Returns:
            tuple: (要不要发言；裁判建议的推理档位或 None——None 表示回落规则 route())
        """
        if not self._gate_ctl.should_consult():
            return True, None

        decision = await self._gate_ctl.decide(snapshot, turn)
        if self._gate.enabled and not decision.reply:
            if self._energy is not None:
                self._energy.note_skip(decision.source)
            self._remember_user(snapshot)
            await self.health.record_metric("gate.skip", 1.0, unit="次")
            return False, None
        if self._gate.route_by_model and decision.effort is not None:
            return True, tier_from_deep_score(decision.effort, self._gate.deep_cuts)
        return True, None

    def _remember_user(self, snapshot: SceneSnapshot) -> None:
        """门控跳过时只把用户这句话写进记忆（不回复≠没听过，保证连续性）。"""
        item = MemoryItem(
            topic="对话",
            content=f"{snapshot.sender}: {snapshot.content}",
            memory_type=MemoryType.DIALOGUE,
        )
        self._tasks.spawn(
            self.bus.publish(EventBus.new(EventTopic.MEMORY_WRITE, source="gate", payload=item)),
            name="memory-write",
        )

    def _session_health(self) -> dict[str, float]:
        """会话摘要（供健康报告采集；作为回调注入，避免 core/health 反向依赖）。"""
        return {
            "total": float(len(self.sessions.owners())),
            "context_usage": self.sessions.context_usage("responder"),
        }

    async def _on_context_pressure(self, alert) -> None:
        """干预：上下文占用过高 → 立刻做一次记忆蒸馏，给窗口腾地方。"""
        value = alert.metric.value if alert.metric else 0.0
        self._logger.warning(f"上下文占用过高（{value:.0%}），触发记忆蒸馏")
        await self._distill()

    async def _distill(self) -> None:
        """记忆蒸馏：把最早一批工作记忆压缩成摘要条目，然后遗忘原文。

        摘要 importance=0.7，入库即自动落长期记忆；代际令牌校验防止
        关闭后的迟到写入。
        """
        gen = self._runtime.generation
        try:
            old = self.memory.oldest(self._distill_batch)
            if not old:
                return
            summary = await self.compressor.compress(old)
            if not summary or gen != self._runtime.generation:
                return
            item = MemoryItem(
                topic="对话摘要",
                content=summary,
                memory_type=MemoryType.FACT,
                importance=0.7,
            )
            await self.memory.store(item)
            removed = self.memory.forget([m.memory_id for m in old])
            self._logger.info(f"记忆蒸馏: {removed} 条 -> 摘要 {len(summary)} 字")
        except Exception:
            self._logger.exception("记忆蒸馏失败（不影响主链路）")

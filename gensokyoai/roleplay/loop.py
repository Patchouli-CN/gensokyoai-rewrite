"""角色扮演主循环"""

import asyncio
import random
import re
import time
from collections import deque
from pathlib import Path

from ..core.brain.engine import BrainEngine, build_tool_directive, route
from ..core.brain.gate import (
    GateDecision,
    Judge,
    PresenceTracker,
    decide,
    tier_from_deep_score,
)
from ..core.brain.ooc_detector import OOCDetector
from ..core.brain.ooc_judge import audit_with_judge
from ..core.brain.pipeline import ThinkPipeline
from ..core.clock import BiologicalClock
from ..core.config import GateSettings, OOCJudgeSettings, SearchSettings, StyleSettings
from ..core.event_bus import EventBus
from ..core.health import HealthMonitor
from ..core.lifecycle import LifecycleManager
from ..core.memorizer.compressor import Compressor
from ..core.memorizer.embedder import Embedder
from ..core.memorizer.knowledge import KnowledgeCache
from ..core.memorizer.manager import MemoryManager
from ..core.persistence import JsonFilePersistence, PersistenceBackend
from ..core.registry import ToolRegistry
from ..core.responder.anti_parrot import (
    PARROT_REASON,
    avoid_hint,
    ending_key,
    should_retry,
    should_strip_ending,
    similarity,
    strip_ending,
)
from ..core.responder.generator import Responder
from ..core.session_manager import SessionManager
from ..mouth.base import Mouth
from ..mouth.console import ConsoleMouth
from ..satori.perceiver import Perceiver
from ..schemas.brain_schema import BrainConclusion, BrainThinkEffort
from ..schemas.event_schema import BaseEvent, EventTopic, TurnEndPayload
from ..schemas.memory_schema import MemoryItem, MemoryType
from ..schemas.model_schema import ToolSpec
from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager
from ..utils.tasks import TaskManager
from ..utils.text import strip_control_chars
from .character import Character
from .initiative import describe_silence, evaluate_initiative
from .persistence import CharacterStateCodec, SessionPersister
from .trace import ReasoningTrace

EXIT_WORDS = {"exit", "quit", "q", "退出"}

_SUSPICIOUS_BARE = re.compile(r"^[0-9+\-*/().=\s]+$")
""" 纯数字/符号短回复：疑似被用户消息夹带的指令带跑（也可能是冷面接梗——
    只标记不阻断，定夺交给带上下文的 jev 出戏审查）"""

_INJECTION_PATTERNS = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|prompts?|rules?)"
    r"|you\s+are\s+now\b"
    r"|(?:从现在开始|此刻起)你(?:是|变成|充当)"
    r"|无视.{0,10}(?:指令|设定|指示|提示词)"
    r"|(?:repeat|print|show|reveal|输出|打印|重复|显示).{0,24}"
    r"(?:system\s*prompt|提示词|系统指令)",
    re.IGNORECASE,
)
""" 提示注入句型（指令改写 / 泄题）。命中只抬思考档位，不改文案——
    误报代价仅一回合延迟，漏报代价是人设被带跑 """

_EFFORT_ORDER = {
    BrainThinkEffort.NONE: 0,
    BrainThinkEffort.LOW: 1,
    BrainThinkEffort.MID: 2,
    BrainThinkEffort.HIGH: 3,
    BrainThinkEffort.MAX: 4,
}
""" 档位全序（抬下限/比较用）"""


def _effort_order(effort: BrainThinkEffort) -> int:
    """档位的全序号。"""
    return _EFFORT_ORDER[effort]


_STALL_EFFORTS = {BrainThinkEffort.HIGH, BrainThinkEffort.MAX}
""" 值得垫过渡语的档位：多轮接力思考，延迟肉眼可见 """


class _WorldRuntimeCodec:
    """世界**跨回合**运行时状态的持久化编解码。

    这类状态不随回合清空，重启后归零会造成可感知的行为断层：

    - `effort_floor`：OOC 干预抬高的推理档位下限，丢了就退回默认路由
    - `stall_last_turn`：上次垫过渡语的回合号（回合号本身可续计，故可持久化）
    - `distill_counter`：距下次记忆蒸馏的回合计数

    **不持久化 `stall_last_time`**：它取 `time.monotonic()`，跨进程没有意义。
    恢复时直接置为「现在」，等价于重启后重新计时最小间隔 —— 比存一个
    会误导的数值正确。
    """

    key = "world_runtime"

    def __init__(self, world: TouhouWorld) -> None:
        """初始化。

        Args:
            world: 宿主世界（读写的都是其跨回合私有状态）
        """
        self._world = world

    def dump(self) -> dict:
        """导出跨回合运行时状态。

        Returns:
            dict: 档位下限（无则 None）+ 过渡语回合号 + 蒸馏计数
        """
        floor = self._world._effort_floor
        return {
            "effort_floor": floor.value if floor is not None else None,
            "stall_last_turn": self._world._stall_last_turn,
            "distill_counter": self._world._distill_counter,
        }

    def load(self, data) -> None:
        """恢复跨回合运行时状态（字段缺失或非法时保持默认）。

        Args:
            data: dump() 产出的字典；None 时跳过
        """
        if not isinstance(data, dict):
            return
        raw_floor = data.get("effort_floor")
        if raw_floor:
            try:
                self._world._effort_floor = BrainThinkEffort(raw_floor)
            except ValueError:
                self._world._logger.warning(f"档位下限非法，跳过: {raw_floor!r}")
        turn = data.get("stall_last_turn")
        if isinstance(turn, int):
            self._world._stall_last_turn = turn
        counter = data.get("distill_counter")
        if isinstance(counter, int):
            self._world._distill_counter = counter
        # monotonic 时刻跨进程无意义：以「现在」为起点重新计时
        self._world._stall_last_time = time.monotonic()


class TouhouWorld:
    """
    扮演主循环 (Role-Play Loop)
    负责协调 Eyes, Brain, Responder 和 Memorizer 的完整生命周期。
    支持生命周期管理（启动/关闭回调）、记忆蒸馏、主动发言（对话欲）、
    深思考过渡语与 OOC 守门/后置深审、jev 式发言门控（见 core/brain/gate.py）。
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
        ooc_judge: Judge | None = None,
        ooc_judge_settings: OOCJudgeSettings | None = None,
        style: StyleSettings | None = None,
        search: SearchSettings | None = None,
        trace_steps: bool = True,
        tool_timeout: float = 10.0,
        tool_max_result_chars: int = 2000,
        session_id: str = "default",
        storage_dir: str | Path = "data",
        persistence=None,
        embedder: Embedder | None = None,
        memory_min_score: float = 0.0,
        shutdown_drain_timeout: float = 2.0,
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
            ooc_judge: 出戏审查裁判（jev 化多问概率；None 回退旧单点 audit）
            ooc_judge_settings: jev 化出戏审配置（阈值/预算；None = 默认）
            style: 文风防复读配置（None = StyleSettings() 默认）
            search: 联网工具配置（可信知识站表 -> 脑内工具指令；None = 无指令）
            session_id: 会话标识，记忆与会话快照按它隔离（多群/多用户各自一个 id）
            storage_dir: 持久化根目录
            persistence: 可插拔持久化后端；None 用默认 JsonFilePersistence(storage_dir)
            embedder: 记忆向量化器（None = 长期记忆检索退回子串匹配）
            memory_min_score: 语义检索相似度下限（0 = 不过滤；config.embedding.min_score 传入）
            shutdown_drain_timeout: 关闭时等待后台侧链收尾的秒数，超时则取消
        """
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

        # --- 健康干预：把「监控」接成「动作」（装配层注册，core/health 不反向依赖）---
        self.health.register_intervention("session.context_usage", self._on_context_pressure)
        self.health.register_intervention("ooc.rate", self._on_ooc_spike)
        self._effort_floor: BrainThinkEffort | None = None
        """ OOC 飙升时抬高的推理档位下限；审计恢复健康后清除 """
        self._last_total_tokens = 0
        """ 上次采集的累计 token，用于算回合增量 """
        self._last_cost: dict[str, float] = {}
        """ 上次采集的累计费用（币种 -> 金额），用于算回合增量 """

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
            tool_timeout=tool_timeout,
            tool_max_result_chars=tool_max_result_chars,
            pipeline=self._build_think_pipeline(),
            persona_brief=self._persona_brief,
            tool_directive=build_tool_directive((search or SearchSettings()).knowledge_sites),
        )

        self.responder = Responder(sessions=self.sessions, persona=character.prompt)

        # --- 发言门控（jev 式混合门控；enabled=False 时维持「每条都回」旧行为）---
        self._gate = gate if gate is not None else GateSettings()
        self._judge = judge
        self._presence = PresenceTracker(window_s=self._gate.presence_window_s)
        """ 活跃度统计：喂给裁判的 bot_activity（防刷屏靠裁判自觉，不设硬冷却） """

        # --- 主动发言 / 蒸馏旋钮 ---
        self._distill_every = distill_every
        self._distill_batch = distill_batch
        self._initiative_interval = initiative_interval
        self._idle_threshold = idle_threshold
        self._urge_threshold = urge_threshold

        # --- 生物钟：定时任务调度（首个住户 = 主动发言节拍）---
        self.clock = BiologicalClock()
        self.clock.every("initiative", self._initiative_interval, self._maybe_initiative)

        # --- 过渡语 / OOC 旋钮 ---
        self._stall_probability = stall_probability
        self._stall_cooldown_turns = stall_cooldown_turns
        self._stall_min_interval = stall_min_interval
        self._ooc_retry = ooc_retry
        self._ooc_audit = ooc_audit
        self._ooc_judge = ooc_judge
        """ jev 化出戏审查裁判（与门控共用 Judge 协议；None = 回退旧单点 audit） """
        self._ooc_judge_cfg = ooc_judge_settings or OOCJudgeSettings()
        self._style = style or StyleSettings()
        """ 文风防复读配置 """
        self._last_reply = ""
        """ 上一轮回复原文（防复读提示/相似度检测的比对基准） """
        self._recent_endings: deque[str] = deque(maxlen=self._style.ending_window)
        """ 近期收尾指纹窗口（ported qqbot「不复读结尾」规则） """
        self._shutdown_drain_timeout = shutdown_drain_timeout
        """ 关闭时等后台侧链收尾的秒数 """

        # --- 运行时状态 ---
        self._generation = 0
        """ 代际令牌：后台任务（蒸馏/主动发言）持任务发起时的代际，
        回写前校验，防止关闭/重置后的迟到写入污染新会话 """
        self._last_activity = time.monotonic()
        self._distill_counter = 0
        self._busy = False
        """ 主链路生成中标志：避免主动发言与用户回复并发抢同一个 responder 会话 """
        self._stall_last_turn = -(10**9)
        """ 上次垫过渡语的回合号（冷却门控用）"""
        self._stall_last_time = float("-inf")
        """ 上次垫过渡语的时刻（冷却门控用）"""

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
            codecs=[CharacterStateCodec(character), _WorldRuntimeCodec(self)],
        )
        """ 会话持久化器（启动 restore / 事件驱动保存 / 关闭 flush）"""

        self.trace = ReasoningTrace(storage_dir, session_id, enabled=trace_steps)
        """ 思考轨迹留档（每回合一行 JSONL，含逐轮 ReasoningStep）"""

        # 注册默认生命周期回调
        self._register_default_lifecycle()

        self.restored = False
        """ 启动时是否成功恢复了历史会话 """

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
                if getattr(self.eye, "_stop_requested", False):
                    self._logger.info("感知器已停止，主循环退出")
                    break

                # 1. 感知阶段 (Eyes)
                snapshot = await self.eye.next_snapshot()
                if snapshot is None:
                    # 如果是因为停止请求返回 None，直接退出
                    if getattr(self.eye, "_stop_requested", False):
                        break
                    continue

                # 退出信号检查
                if snapshot.content.lower() in EXIT_WORDS:
                    break

                turn += 1
                t_start = time.monotonic()
                self._last_activity = time.monotonic()
                self._presence.record(from_bot=False)
                self._busy = True
                try:
                    # 2. System-1 层：一次裁判调用回答「该不该发言」+「该想多深」
                    proceed, judged = await self._system1_turn(snapshot, turn)
                    if not proceed:
                        continue

                    # 3. 决策阶段 (Brain)；深思考前先垫一句角色过渡语遮延迟
                    # 以当前消息为关联词，顺带把长期记忆里的相关旧事捞进来（语义检索）
                    memories = await self.memory.recent(5, search_term=snapshot.content)
                    effort = self._apply_effort_floor(
                        judged if judged is not None else route(snapshot)
                    )
                    effort = self._apply_injection_floor(snapshot, effort)
                    await self._maybe_stall(snapshot, effort, turn)
                    conclusion = await self.brain.think(snapshot, memories, effort)

                    # 4. 表达 + 投递：口层支持流式则逐块显示，否则缓冲投递（流式下跳过 OOC 预审）
                    reply = await self._express(snapshot, conclusion, memories)
                finally:
                    self._busy = False

                # 5. 记录（投递已在 _express 内完成）
                self._remember_turn(snapshot, reply)
                if self.trace.enabled:
                    # 一次小追加（走线程池），保证本回合轨迹已落盘、便于复盘与测试
                    await self.trace.record(
                        turn=turn, effort=effort.value, conclusion=conclusion, reply=reply
                    )
                if self._ooc_audit and not self._ooc_blocking:
                    # 传 snapshot：jev 化审查需要诱发消息当上下文（旧 audit 只用 reply）。
                    # blocking 模式已在 _express 里审过并记账，侧链不重复跑
                    self._tasks.spawn(self._audit_reply(snapshot, reply), name="ooc-audit")

                # 6. 角色状态跟踪 + 健康喂食
                if conclusion.emotion:
                    self.character.status.update(emotion=conclusion.emotion)
                turn_cost = await self._record_health(turn, effort, t_start)

                # 7. 记忆蒸馏（后台侧链，不阻塞回合）
                self._distill_counter += 1
                if self._distill_counter >= self._distill_every:
                    self._distill_counter = 0
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
            self._generation += 1
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

    def _consult_judge(self) -> bool:
        """有裁判、且（开了门控 或 开了模型路由）时才问裁判。"""
        return self._judge is not None and (self._gate.enabled or self._gate.route_by_model)

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
        if not self._consult_judge():
            return True, None

        decision = await self._gate_decide(snapshot, turn)
        if self._gate.enabled and not decision.reply:
            self._remember_user(snapshot)
            await self.health.record_metric("gate.skip", 1.0, unit="次")
            return False, None
        if self._gate.route_by_model and decision.effort is not None:
            return True, tier_from_deep_score(decision.effort, self._gate.deep_cuts)
        return True, None

    async def _gate_decide(self, snapshot: SceneSnapshot, turn: int) -> GateDecision:
        """只做 decide + 日志（无副作用；跳过记账由 _system1_turn 按门控开关决定）。"""
        recent = await self.memory.recent(5)
        recent_texts = [m.content for m in reversed(recent)]
        decision = await decide(
            snapshot=snapshot,
            bot_name=self.character.name,
            persona=self._persona_brief,
            recent=recent_texts,
            presence=self._presence.stats(),
            judge=self._judge,
            group_threshold=self._gate.group_threshold,
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
            f"({decision.source}: {decision.reason}){scores}{deep} :: "
            f"{snapshot.sender}: {snapshot.content[:60]}"
        )
        return decision

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

    async def _record_health(self, turn: int, effort, t_start: float) -> dict[str, float]:
        """回合粒度的健康指标喂食：档位分布 / 记忆规模 / 延迟 / token / 费用 / 上下文占用率。

        Returns:
            dict: 本回合新增费用（币种 -> 金额；本地模型为空）
        """
        total = self.sessions.total_usage()
        turn_tokens = (total.prompt_tokens + total.completion_tokens) - self._last_total_tokens
        self._last_total_tokens = total.prompt_tokens + total.completion_tokens
        turn_cost = self._turn_cost()

        # 注意：`HealthMonitor.record()` 只收**单条**指标（{"name", "value"}），
        # 曾经把整本计数器 dict 塞进去，被「指标缺少 name」静默丢弃 ——
        # 档位分布与 memory.* 阈值因此从未生效。逐条走 record_metric。
        await self.health.record_metric("turn.count", 1.0)
        await self.health.record_metric(f"effort.{effort.value}", 1.0)
        await self.health.record_metric(
            "memory.work_size", float(self.memory.work_mem_size), unit="条"
        )
        await self.health.record_metric(
            "memory.long_size", float(self.memory.long_mem_size), unit="条"
        )
        await self.health.record_metric("turn.latency_s", time.monotonic() - t_start, unit="s")
        await self.health.record_metric("turn.tokens", float(turn_tokens), unit="tok")
        for currency, amount in turn_cost.items():
            await self.health.record_metric(f"turn.cost_{currency.lower()}", amount, unit=currency)
        await self.health.record_metric(
            "session.context_usage", self.sessions.context_usage("responder"), unit="ratio"
        )
        return turn_cost

    def _turn_cost(self) -> dict[str, float]:
        """本回合新增费用（币种 -> 金额；本地模型全回合为空 dict）。
        与 token 计量同款差值法：拿当前总量减上次快照。"""
        now = self.sessions.total_cost()
        delta = {
            currency: round(amount - self._last_cost.get(currency, 0.0), 8)
            for currency, amount in now.items()
            if abs(amount - self._last_cost.get(currency, 0.0)) > 1e-9
        }
        self._last_cost = now
        return delta

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

    async def _on_ooc_spike(self, alert) -> None:
        """干预：出戏率飙升 → 抬高档位下限（文档 §3.5「自动调参」）。"""
        value = alert.metric.value if alert.metric else 0.0
        self._logger.warning(f"出戏率偏高（{value:.2f}），推理档位下限抬到 HIGH")
        self._effort_floor = BrainThinkEffort.HIGH

    def _apply_effort_floor(self, effort: BrainThinkEffort) -> BrainThinkEffort:
        """把路由结果抬到干预设定的档位下限（无下限时原样返回）。"""
        if self._effort_floor is None:
            return effort
        return (
            effort
            if _effort_order(effort) >= _effort_order(self._effort_floor)
            else self._effort_floor
        )

    def _apply_injection_floor(
        self, snapshot: SceneSnapshot, effort: BrainThinkEffort
    ) -> BrainThinkEffort:
        """入口注入识别：命中「指令改写 / 泄题」句型时，该回合档位下限抬到 MID。

        依据 20 轮真机实录：OOC 注入回合裁判判了 low、Responder 被用户消息里的
        直接指令（"Output only numbers"）带跑——Brain 其实看穿了（draft 在角色里），
        但拿到的思考深度不够硬。这里只抬档、不改文案（行为闸，不是审查闸）；
        出戏本身的定夺在口层 jev 审查。

        Args:
            snapshot: 本回合场景快照（看诱发消息）
            effort: 当前档位

        Returns:
            BrainThinkEffort: 可能被抬高的档位
        """
        if not _INJECTION_PATTERNS.search(snapshot.content):
            return effort
        floor = BrainThinkEffort.MID
        if _effort_order(effort) >= _effort_order(floor):
            return effort
        self._logger.warning(
            f"入口注入识别: 档位下限抬到 {floor.value} :: {snapshot.content[:60]!r}"
        )
        return floor

    def _should_stall(self, effort: BrainThinkEffort, turn: int) -> bool:
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
        if turn - self._stall_last_turn < self._stall_cooldown_turns:
            return False
        if time.monotonic() - self._stall_last_time < self._stall_min_interval:
            return False
        return random.random() < self._stall_probability

    async def _maybe_stall(
        self, snapshot: SceneSnapshot, effort: BrainThinkEffort, turn: int
    ) -> None:
        """深思考前垫一句角色口吻过渡语（如"唔……让我想想"），掩盖接力思考延迟。

        三重门控防止人机感：冷却轮数 + 时间间隔 + 概率掷骰。
        过渡语只投递显示层，不写记忆（对 Brain 是噪音）；
        但它进了 responder 有状态会话，正式回复能看到它、自然承接不重复。
        """
        if not self._should_stall(effort, turn):
            return
        try:
            line = await self.responder.stall(snapshot)
        except Exception:
            self._logger.exception("过渡语生成失败（跳过，不影响主链路）")
            return
        if not line:
            return
        self._stall_last_turn = turn
        self._stall_last_time = time.monotonic()
        await self.mouth.send(self.character.name, strip_control_chars(line))
        self._logger.info(f"过渡语已投递: {line!r}")

    async def _express(self, snapshot, conclusion, memories, *, ooc_guard: bool = True) -> str:
        """表达 + 投递的统一入口：口层支持流式则逐块显示，否则缓冲投递。

        主循环与主动发言共用；主动发言传 `ooc_guard=False`（不做硬规则守门，
        但防复读与可疑标记照跑——它也是「说话」）。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论（主动发言为 pass_through 规则结论）
            memories: 检索到的记忆
            ooc_guard: 缓冲路径是否做 OOC 硬规则守门（流式路径恒不做，
                靠后置 jev 审查兜底——已投递的文本撤不回，重在记录与干预）

        Returns:
            str: 完整回复文本（供记忆落盘 / 后置审查）
        """
        # 防复读预防性提示：两条路径都生效（流式已投递无从改起，只能事前防）
        avoid = self._parrot_avoid_hint()
        # blocking 闸门开启时**放弃流式**：回复必须先完整生成、过 jev 出戏审查
        # 才放行——逐字蹦的手感换「说出口的话都过了审」（本地模型每回合多一次
        # 审查调用；侧链模式则两不误，默认）
        if self.mouth.supports_streaming and not self._ooc_blocking:
            reply = await self._deliver_stream(snapshot, conclusion, memories, avoid=avoid)
            await self._note_suspicious(reply)
            self._note_reply_style(reply)
            return reply
        reply = await self.responder.respond(conclusion, snapshot, memories, avoid)
        if self._ooc_blocking:
            reply = await self._guard_ooc_jev(snapshot, reply)
        if ooc_guard:
            reply = await self._guard_ooc(reply)
            reply = await self._guard_parrot(reply)
        await self._note_suspicious(reply)
        final = self._dedup_ending(reply)
        await self.mouth.send(self.character.name, strip_control_chars(final))
        self._presence.record(from_bot=True)
        self._note_reply_style(reply)
        return final

    async def _deliver_stream(self, snapshot, conclusion, memories, avoid: str = "") -> str:
        """流式投递最终回复：responder.respond_stream → mouth.begin/delta/end。

        逐块把回复文本送到可显示的平台（口层流式），并返回完整回复文本
        （供记忆落盘 / 后置 jev 审查 / 状态回写）。流式模式下跳过 OOC 预审。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论
            memories: 检索到的记忆
            avoid: 防复读提示（生成前注入；空串不注入）

        Returns:
            str: 完整回复文本（含情绪润色尾缀）
        """
        await self.mouth.begin(self.character.name)
        parts: list[str] = []
        try:
            async for delta in self.responder.respond_stream(conclusion, snapshot, memories, avoid):
                parts.append(delta)
                if delta:
                    await self.mouth.delta(strip_control_chars(delta))
        except Exception:
            self._logger.exception("流式生成失败（结束投递，回退为已产出文本）")
        finally:
            await self.mouth.end()
        reply = "".join(parts)
        self._presence.record(from_bot=True)
        self._logger.info(f"流式投递完成: {len(reply)}字")
        return reply

    @property
    def _ooc_blocking(self) -> bool:
        """blocking 闸门是否生效（最终缓冲区审查通过才放行）。

        生效条件三合一：配置了裁判 + jev 审查 enabled + mode=blocking。
        生效时 `_express` 放弃流式（先完整生成、审过再发）。
        """
        return (
            self._ooc_judge is not None
            and self._ooc_judge_cfg.enabled
            and self._ooc_judge_cfg.mode == "blocking"
        )

    async def _guard_ooc_jev(self, snapshot: SceneSnapshot, reply: str) -> str:
        """blocking 闸门：进最终缓冲区的回复先过 jev 出戏审查，通过才放行。

        判定 revise（双高/unsafe/implausible 低线）时花一次纠偏重写；
        纠偏后仍命中硬规则则保留原句 + 告警（不死循环）。
        flag（模糊带）与原句都直接放行——黄色预警不该有阻断权。

        Args:
            snapshot: 场景快照（诱发消息进审查 state）
            reply: 待放行的回复

        Returns:
            str: 审查（+可能纠偏）后的回复
        """
        if not self._ooc_blocking:
            return reply
        judge = self._ooc_judge
        assert judge is not None  # _ooc_blocking 已保证非空（mypy 收窄）
        try:
            recent = await self.memory.recent(5)
            check = await audit_with_judge(
                judge,
                persona=self._persona_brief,
                new_message=snapshot.content,
                reply=reply,
                recent=[m.content for m in reversed(recent)],
                bot_name=self.character.name,
                settings=self._ooc_judge_cfg,
            )
        except Exception:
            # 审查自身故障 = 放行（防御不是裁判，不能让一次故障吞掉回合）
            self._logger.exception("blocking 出戏审查失败，放行")
            return reply
        if check.decision != "revise":
            if check.decision == "flag":
                self._logger.info(f"blocking 出戏审查 flag（放行）: {check.reason}")
            await self._record_ooc_check(check)
            return reply
        self._logger.warning(
            f"blocking 出戏审查 revise，发起一次纠偏: {check.reason} | {check.answers}"
        )
        try:
            corrected = await self.responder.correct(reply, f"出戏原因: {check.reason}")
        except Exception:
            self._logger.exception("blocking 纠偏重生成失败，保留原句")
            await self._record_ooc_check(check)
            return reply
        if not corrected or self.ooc.pre_filter(corrected).is_ooc:
            self._logger.error("纠偏后仍不可用，原样放行")
            await self._record_ooc_check(check)
            return reply
        self._logger.info(f"blocking 纠偏完成: {corrected[:60]!r}")
        await self._record_ooc_check(check)
        return corrected

    async def _record_ooc_check(self, check) -> None:
        """jev 审查结论记账（blocking 与侧链共用）：ooc_hits/ooc_flags/ooc.rate +
        档位下限自愈。revise 计入 hits（出戏率），flag 只计 flags 不进率——
        模糊带的判定不该把出戏率推高触发误干预。"""
        audited = int(self.character.status.extra.get("ooc_audited", 0)) + 1
        hits = int(self.character.status.extra.get("ooc_hits", 0)) + (
            1 if check.decision == "revise" else 0
        )
        flags = int(self.character.status.extra.get("ooc_flags", 0)) + (
            1 if check.decision == "flag" else 0
        )
        self.character.status.update(ooc_audited=audited, ooc_hits=hits, ooc_flags=flags)
        await self.health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
        if check.decision != "revise" and self._effort_floor is not None:
            # 审查恢复健康 -> 撤销干预抬高的档位下限（自愈，不长期烧算力）
            self._effort_floor = None
            self._logger.info("OOC 已恢复健康，撤销抬高的推理档位下限")

    def _parrot_avoid_hint(self) -> str:
        """防复读预防性提示（responder.user 的 [自我克制] 段）。
        Returns:
            str: 提示文本；上一轮无发言且无重复收尾时为空串（不注入）
        """
        return avoid_hint(self._last_reply, self._recent_endings)

    async def _guard_parrot(self, reply: str) -> str:
        """相邻轮相似度过阈值 -> 一次防复读纠偏重写（缓冲路径）。

        流式路径文本已在投递中、无从改起，只做事前提示（见 _parrot_avoid_hint）；
        这里兜底覆盖缓冲投递（CLI / 非流式口层）。

        Args:
            reply: 本轮新生成的回复

        Returns:
            str: 守门后的回复（重写更优返回新文本，否则原样）
        """
        if not should_retry(reply, self._last_reply, self._style.similarity_retry):
            return reply
        ratio = similarity(reply, self._last_reply)
        self._logger.warning(f"防复读守门: 与上轮相似度 {ratio:.0%} 超阈值，发起一次重写")
        try:
            rewritten = await self.responder.correct(reply, PARROT_REASON)
        except Exception:
            self._logger.exception("防复读重写失败，保留原句")
            return reply
        if rewritten and similarity(rewritten, self._last_reply) < ratio:
            return rewritten
        self._logger.info("防复读重写后仍高于原相似度，保留原句")
        return reply

    def _dedup_ending(self, reply: str) -> str:
        """收尾去重（ported qqbot「不复读结尾」规则）：同一收尾在近期窗口
        已出现 ≥2 次就剥掉它。确定性规则，零 token；一句话的回复不剥。"""
        if not self._style.dedup_endings:
            return reply
        if should_strip_ending(reply, list(self._recent_endings)):
            stripped = strip_ending(reply)
            if stripped:
                self._logger.info(f"收尾去重: 剥掉反复使用的收尾 {ending_key(reply)!r}")
                return stripped
        return reply

    def _note_reply_style(self, reply: str) -> None:
        """更新防复读状态：比对基准 + 收尾指纹窗口（记原始收尾——倾向追踪，
        模型想用什么梗是它的本能，剥不剥是我们的事）。"""
        self._last_reply = reply
        if self._style.dedup_endings:
            self._recent_endings.append(ending_key(reply))

    async def _note_suspicious(self, reply: str) -> None:
        """零成本启发式：纯数字/符号超短回复 = 疑似被用户消息里夹带的指令带跑
        （20 轮实录的 OOC 注入就是回了个「4」）。

        **只标记不阻断**：这也可能是合法的冷面接梗，自动纠偏会误杀——
        定夺交给带上下文的 jev 出戏审查（blocking 模式下才可能在出口拦下）。
        """
        core = reply.strip()
        if not core or len(core) > 10 or not _SUSPICIOUS_BARE.match(core):
            return
        flags = int(self.character.status.extra.get("ooc_suspicious", 0)) + 1
        self.character.status.update(ooc_suspicious=flags)
        await self.health.record_metric("ooc.suspicious", 1.0, unit="次")
        self._logger.warning(f"疑似被注入带跑的短回复（已标记，待 jev 审查定夺）: {reply!r}")

    async def _guard_ooc(self, reply: str) -> str:
        """最终回复的 OOC 规则守门：零成本快筛，命中才花一次纠偏重生成。

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
        self._logger.warning(f"最终回复命中 OOC 规则（{hit.reason}），发起一次纠偏")
        try:
            corrected = await self.responder.correct(reply, hit.reason)
        except Exception:
            self._logger.exception("OOC 纠偏重生成失败，原样输出")
            return reply
        if corrected and not self.ooc.pre_filter(corrected).is_ooc:
            flags = int(self.character.status.extra.get("ooc_flags", 0)) + 1
            self.character.status.update(ooc_flags=flags)
            return corrected
        self._logger.error(f"纠偏后仍命中 OOC，原样输出: {corrected[:60]!r}")
        return reply

    async def _audit_reply(self, snapshot: SceneSnapshot, reply: str) -> None:
        """后置出戏审查（异步侧链，不阻塞回复）。

        两条路径：
        - **jev 化**（`ooc_judge` 可用且 enabled）：多问概率 + 接受规则，
          state 带**诱发消息**——「服从了指令的形式」与「丢了角色的魂」
          分开打分，冷面接梗不再被一刀切判死（20 轮真机实录照出的旧盲区）；
        - 旧单点 audit（无裁判时的降级）：JSON 布尔判定，只看人设+回复。

        两条路径都**不撤回已发出的文本**（流式已投递，撤不回），只回写角色
        状态与健康指标（`ooc.rate` 超阈值时 HealthMonitor 自动抬高推理档位）。

        Args:
            snapshot: 本回合场景快照（取诱发消息进审查 state）
            reply: 已发出的最终回复
        """
        if self._ooc_judge is not None and self._ooc_judge_cfg.enabled:
            await self._audit_reply_jev(snapshot, reply)
            return
        await self._audit_reply_legacy(reply)

    async def _audit_reply_jev(self, snapshot: SceneSnapshot, reply: str) -> None:
        """jev 化出戏审查侧链：多问概率 -> 三档结论 -> 记账/干预。"""
        judge = self._ooc_judge
        if judge is None:
            return
        gen = self._generation
        try:
            recent = await self.memory.recent(5)
            check = await audit_with_judge(
                judge,
                persona=self._persona_brief,
                new_message=snapshot.content,
                reply=reply,
                recent=[m.content for m in reversed(recent)],
                bot_name=self.character.name,
                settings=self._ooc_judge_cfg,
            )
        except Exception:
            self._logger.exception("jev 出戏审查失败（不影响主链路）")
            return
        if gen != self._generation:
            return
        if check.decision == "revise":
            self._logger.warning(
                f"jev 出戏审查 revise（已记账，文本不撤回）: {check.reason} | {check.answers}"
            )
        elif check.decision == "flag":
            self._logger.info(f"jev 出戏审查 flag: {check.reason} | {check.answers}")
        await self._record_ooc_check(check)

    async def _audit_reply_legacy(self, reply: str) -> None:
        """旧单点 OOC 深审（无 jev 裁判时的降级路径；只看人设+回复，无诱发消息）。"""
        gen = self._generation
        try:
            verdict = await self.ooc.audit(reply, self.character.prompt)
            if gen != self._generation:
                return
            audited = int(self.character.status.extra.get("ooc_audited", 0)) + 1
            hits = int(self.character.status.extra.get("ooc_hits", 0)) + (
                1 if verdict.is_ooc else 0
            )
            self.character.status.update(ooc_audited=audited, ooc_hits=hits)
            await self.health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
            if not verdict.is_ooc and self._effort_floor is not None:
                # 审计恢复健康 -> 撤销干预抬高的档位下限（自愈，不长期烧算力）
                self._effort_floor = None
                self._logger.info("OOC 已恢复健康，撤销抬高的推理档位下限")
        except Exception:
            self._logger.exception("OOC 深审失败（不影响主链路）")

    async def _distill(self) -> None:
        """记忆蒸馏：把最早一批工作记忆压缩成摘要条目，然后遗忘原文。

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

    async def _maybe_initiative(self) -> None:
        """主动发言节拍（生物钟住户）：空闲超阈值时评估四维对话欲，达标即开口。

        零思考 token：不经过 Brain，直接用规则结论驱动 Responder。
        """
        if self._busy or self._stopping():
            return
        idle = time.monotonic() - self._last_activity
        if idle < self._idle_threshold:
            return

        try:
            await self._try_speak(idle)
        except Exception:
            self._logger.exception("主动发言评估失败")

    def _stopping(self) -> bool:
        """是否处于关闭流程"""
        return getattr(self.eye, "_stop_requested", False)

    async def _try_speak(self, idle: float) -> None:
        """评估对话欲并尝试主动开口"""
        gen = self._generation
        recent = await self.memory.recent(6)
        recent_texts = [m.content for m in reversed(recent)]
        urge = evaluate_initiative(
            recent_texts,
            character_name=self.character.name,
            weights=self.character.card.motivation_weights,
            idle_seconds=idle,
            expression_base=self.character.card.expression_base,
        )
        self.character.status.update(motivation=round(urge, 2))
        if urge < self._urge_threshold:
            return
        if self._busy or gen != self._generation:
            return

        self._logger.info(f"主动发言触发: 对话欲={urge:.2f} 空闲={idle:.0f}s")
        self._busy = True
        try:
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
                emotion=self.character.status.emotion,
            )
            # 与主循环同一套投递：口层支持流式则逐块显示，否则缓冲投递（主动发言不做 OOC 守门）
            reply = await self._express(snapshot, conclusion, recent, ooc_guard=False)
        finally:
            self._busy = False

        char_mem = MemoryItem(
            topic="对话",
            content=f"{self.character.name}: {reply}",
            memory_type=MemoryType.DIALOGUE,
        )
        await self.bus.publish(
            EventBus.new(EventTopic.MEMORY_WRITE, source="initiative", payload=char_mem)
        )
        self._last_activity = time.monotonic()

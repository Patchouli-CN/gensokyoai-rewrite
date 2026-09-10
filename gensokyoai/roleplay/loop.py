"""角色扮演主循环"""

import asyncio
import contextlib
import random
import time
from pathlib import Path

from ..core.brain.engine import BrainEngine, route
from ..core.brain.ooc_detector import OOCDetector
from ..core.event_bus import EventBus
from ..core.health import HealthMonitor
from ..core.lifecycle import LifecycleManager
from ..core.memorizer.compressor import Compressor
from ..core.memorizer.manager import MemoryManager
from ..core.persistence import JsonFilePersistence, PersistenceBackend
from ..core.registry import ToolRegistry
from ..core.responder.generator import Responder
from ..core.session_manager import SessionManager
from ..eyes.perceiver import Perceiver
from ..mouth.base import Mouth
from ..mouth.console import ConsoleMouth
from ..schemas.brain_schema import BrainConclusion, BrainThinkEffort
from ..schemas.event_schema import BaseEvent, EventTopic, TurnEndPayload
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import ToolSpec
from ..schemas.scene_schema import SceneSnapshot
from ..utils.logger import LoggerManager
from ..utils.text import strip_control_chars
from .character import Character
from .initiative import describe_silence, evaluate_initiative
from .persistence import CharacterStateCodec, SessionPersister

EXIT_WORDS = {"exit", "quit", "q", "退出"}

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
                self._world.logger.warning(f"档位下限非法，跳过: {raw_floor!r}")
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
    深思考过渡语与 OOC 守门/后置深审。
    """

    def __init__(
        self,
        eye: Perceiver,
        character: Character,
        sessions: SessionManager,
        mouth: Mouth | None = None,
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
        tool_timeout: float = 10.0,
        tool_max_result_chars: int = 2000,
        session_id: str = "default",
        storage_dir: str | Path = "data",
        persistence=None,
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
            session_id: 会话标识，记忆与会话快照按它隔离（多群/多用户各自一个 id）
            storage_dir: 持久化根目录
            persistence: 可插拔持久化后端；None 用默认 JsonFilePersistence(storage_dir)
        """
        self.logger = LoggerManager.get_logger("TOUHOU WORLD")
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
        self.memory = MemoryManager(storage_dir=storage_dir, session_id=session_id)
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

        self.compressor = Compressor(sessions)

        # --- 组装工具：注册表（内置） + 外部传入 ---
        all_tools = self._setup_tools(external_tools)

        # 初始化业务模块
        self.ooc = OOCDetector(self.sessions)
        self.brain = BrainEngine(
            sessions=self.sessions,
            persona=character.prompt,
            ooc=self.ooc,
            tools=all_tools,
            tool_timeout=tool_timeout,
            tool_max_result_chars=tool_max_result_chars,
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
                self.logger.info(f"=== 幻想乡会话已恢复 | 角色: {self.character.name} ===")
            else:
                self.logger.info(f"=== 幻想乡连接成功 | 角色: {self.character.name} ===")

        @self.lifecycle.on_shutdown
        async def _on_shutdown():
            self.logger.info("=== 幻想乡连接已断开 ===")
            await self.persistence.flush()
            await self.eye.close()
            self.sessions.reset_all()

    def _setup_tools(self, external_tools: list[ToolSpec] | None = None) -> list[ToolSpec]:
        """组装工具列表"""
        tools = []

        try:
            registered_tools = ToolRegistry.all()
            if registered_tools:
                tools.extend(registered_tools)
                self.logger.info(
                    f"加载全局注册工具: {[t.tool_func.__name__ for t in registered_tools]}"
                )
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
        """异步处理记忆存储，不阻塞主链路"""
        if isinstance(event.payload, MemoryItem):
            await self.memory.store(event.payload)

    async def start(self) -> None:
        """启动主循环（开场白 + 主动发言后台任务 + 优雅关闭）"""
        await self.lifecycle.startup()
        if not self.restored:
            await self._greet()
        initiative_task = asyncio.create_task(self._initiative_loop())

        turn = self.persistence.turn_count
        try:
            while True:
                # 检查是否收到停止请求
                if getattr(self.eye, "_stop_requested", False):
                    self.logger.info("感知器已停止，主循环退出")
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
                self._busy = True
                try:
                    # 2. 决策阶段 (Brain)；深思考前先垫一句角色过渡语遮延迟
                    memories = await self.memory.recent(5)
                    effort = self._apply_effort_floor(route(snapshot))
                    await self._maybe_stall(snapshot, effort, turn)
                    conclusion = await self.brain.think(snapshot, memories, effort)

                    # 3. 表达 + 投递：口层支持流式则逐块显示，否则缓冲投递（流式下跳过 OOC 预审）
                    reply = await self._express(snapshot, conclusion, memories)
                finally:
                    self._busy = False

                # 4. 记录（投递已在 _express 内完成）
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
                await self.bus.publish(
                    EventBus.new(
                        EventTopic.TURN_END,
                        source="loop",
                        payload=TurnEndPayload(turn=turn, effort=effort.value, latency_s=latency),
                    )
                )

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

    async def _greet(self) -> None:
        """打出角色开场白（若有）"""
        greeting = self.character.card.greeting
        if greeting:
            await self.mouth.send(self.character.name, greeting)

    def _remember_turn(self, snapshot: SceneSnapshot, reply: str) -> None:
        """把本回合的用户输入与角色回复投递到记忆总线（异步侧链）"""
        user_mem = MemoryItem(
            topic="对话", content=f"{snapshot.sender}: {snapshot.content}", memory_type="dialogue"
        )
        char_mem = MemoryItem(
            topic="对话",
            content=f"{self.character.name}: {reply}",
            memory_type="dialogue",
            relate_ids={user_mem.memory_id},
        )
        for item in [user_mem, char_mem]:
            asyncio.get_running_loop().create_task(
                self.bus.publish(EventBus.new(EventTopic.MEMORY_WRITE, source="loop", payload=item))
            )

    async def _record_health(self, turn: int, effort, t_start: float) -> None:
        """回合粒度的健康指标喂食：档位分布 / 记忆规模 / 延迟 / token / 上下文占用率"""
        total = self.sessions.total_usage()
        turn_tokens = (total.prompt_tokens + total.completion_tokens) - self._last_total_tokens
        self._last_total_tokens = total.prompt_tokens + total.completion_tokens

        await self.health.record(
            {
                "turn.count": 1,
                f"effort.{effort.value}": 1,
                "memory.work_size": self.memory.work_mem_size,
                "memory.long_size": self.memory.long_mem_size,
            }
        )
        await self.health.record_metric("turn.latency_s", time.monotonic() - t_start, unit="s")
        await self.health.record_metric("turn.tokens", float(turn_tokens), unit="tok")
        await self.health.record_metric(
            "session.context_usage", self.sessions.context_usage("responder"), unit="ratio"
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
        self.logger.warning(f"上下文占用过高（{value:.0%}），触发记忆蒸馏")
        await self._distill()

    async def _on_ooc_spike(self, alert) -> None:
        """干预：出戏率飙升 → 抬高档位下限（文档 §3.5「自动调参」）。"""
        value = alert.metric.value if alert.metric else 0.0
        self.logger.warning(f"出戏率偏高（{value:.2f}），推理档位下限抬到 HIGH")
        self._effort_floor = BrainThinkEffort.HIGH

    def _apply_effort_floor(self, effort: BrainThinkEffort) -> BrainThinkEffort:
        """把路由结果抬到干预设定的档位下限（无下限时原样返回）。"""
        if self._effort_floor is None:
            return effort
        order = {
            BrainThinkEffort.OFF: 0,
            BrainThinkEffort.LOW: 1,
            BrainThinkEffort.MID: 2,
            BrainThinkEffort.HIGH: 3,
            BrainThinkEffort.MAX: 4,
        }
        return effort if order[effort] >= order[self._effort_floor] else self._effort_floor

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
            self.logger.exception("过渡语生成失败（跳过，不影响主链路）")
            return
        if not line:
            return
        self._stall_last_turn = turn
        self._stall_last_time = time.monotonic()
        await self.mouth.send(self.character.name, strip_control_chars(line))
        self.logger.info(f"过渡语已投递: {line!r}")

    async def _express(self, snapshot, conclusion, memories, *, ooc_guard: bool = True) -> str:
        """表达 + 投递的统一入口：口层支持流式则逐块显示，否则缓冲投递。

        主循环与主动发言共用；主动发言传 `ooc_guard=False`（本来就不做守门）。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论（主动发言为 pass_through 规则结论）
            memories: 检索到的记忆
            ooc_guard: 缓冲路径是否做 OOC 预审（流式路径恒不做，靠后置深审兜底）

        Returns:
            str: 完整回复文本（供记忆落盘 / 后置深审）
        """
        if self.mouth.supports_streaming:
            return await self._deliver_stream(snapshot, conclusion, memories)
        reply = await self.responder.respond(conclusion, snapshot, memories)
        if ooc_guard:
            reply = await self._guard_ooc(reply)
        await self.mouth.send(self.character.name, strip_control_chars(reply))
        return reply

    async def _deliver_stream(self, snapshot, conclusion, memories) -> str:
        """流式投递最终回复：responder.respond_stream → mouth.begin/delta/end。

        逐块把回复文本送到可显示的平台（口层流式），并返回完整回复文本
        （供记忆落盘 / 后置 OOC 深审 / 状态回写）。流式模式下跳过 OOC 预审。

        Args:
            snapshot: 场景快照
            conclusion: Brain 结论
            memories: 检索到的记忆

        Returns:
            str: 完整回复文本（含情绪润色尾缀）
        """
        await self.mouth.begin(self.character.name)
        parts: list[str] = []
        try:
            async for delta in self.responder.respond_stream(conclusion, snapshot, memories):
                parts.append(delta)
                if delta:
                    await self.mouth.delta(strip_control_chars(delta))
        except Exception:
            self.logger.exception("流式生成失败（结束投递，回退为已产出文本）")
        finally:
            await self.mouth.end()
        reply = "".join(parts)
        self.logger.info(f"流式投递完成: {len(reply)}字")
        return reply

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
        """后置 OOC 深审（异步侧链，不阻塞回复）。

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
            hits = int(self.character.status.extra.get("ooc_hits", 0)) + (
                1 if verdict.is_ooc else 0
            )
            self.character.status.update(ooc_audited=audited, ooc_hits=hits)
            await self.health.record_metric("ooc.rate", hits / max(audited, 1), unit="ratio")
            if not verdict.is_ooc and self._effort_floor is not None:
                # 审计恢复健康 -> 撤销干预抬高的档位下限（自愈，不长期烧算力）
                self._effort_floor = None
                self.logger.info("OOC 已恢复健康，撤销抬高的推理档位下限")
        except Exception:
            self.logger.exception("OOC 深审失败（不影响主链路）")

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
                memory_type="fact",
                importance=0.7,
            )
            await self.memory.store(item)
            removed = self.memory.forget([m.memory_id for m in old])
            self.logger.info(f"记忆蒸馏: {removed} 条 -> 摘要 {len(summary)} 字")
        except Exception:
            self.logger.exception("记忆蒸馏失败（不影响主链路）")

    async def _initiative_loop(self) -> None:
        """主动发言后台循环：空闲超阈值时评估四维对话欲，达标即开口。

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

        self.logger.info(f"主动发言触发: 对话欲={urge:.2f} 空闲={idle:.0f}s")
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
            memory_type="dialogue",
        )
        await self.bus.publish(
            EventBus.new(EventTopic.MEMORY_WRITE, source="initiative", payload=char_mem)
        )
        self._last_activity = time.monotonic()

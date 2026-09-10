"""会话持久化编排 —— 事件驱动的状态落盘与重启恢复。

存什么、何时存归这里，怎么存归 PersistenceBackend（可插拔）。
通信走事件总线：订阅 MEMORY_WRITE / TURN_END 触发保存，
自身不侵入主循环。

**额外状态的形状**（角色运行时状态、世界跨回合旋钮…）通过 `StateCodec` 下放给
装配层注册 —— 本模块不必知道 Character / TouhouWorld 的内部字段。

**关于「思考的中间状态」**：接力思考的中间态（上一轮 thought / 已得行动指令 / 轮次）
是 `_relay_think` 内的**局部变量**，随调用结束即消失。它属于「一次计算的进行时」，
而不是「重启该接着用的状态」—— 进程若在回合中途被杀，那一回合本就未产出回复，
正确做法是**重跑该回合**（Brain 无状态是刻意设计，见架构文档 §11），
而不是把半截思考持久化后「续上」（那会引出「用户消息是否已入记忆 / 是否补发回复」
这类一致性问题，收益为零、复杂度为正）。
"""

import asyncio
import time
from typing import Any, Protocol

import msgspec

from ..core.event_bus import EventBus
from ..core.persistence import PersistenceBackend
from ..core.session_manager import SessionManager
from ..schemas.event_schema import EventTopic
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import Message
from ..utils.logger import LoggerManager
from ..utils.tasks import TaskRegistry

_SCHEMA_VERSION = 2
""" 会话文件格式版本（v2 起增加 character_state / world_runtime 段；旧档缺失时按默认跳过）"""


class StateCodec(Protocol):
    """附加状态编解码器：把「额外存什么」下放给装配层。

    实现只需三个成员：`key`（载荷字段名）、`dump()`、`load(data)`。
    恢复时若版本较旧、字段缺失，`load(None)` 应当是无害的空操作。
    """

    key: str
    """ 在会话载荷中的字段名 """

    def dump(self) -> Any:
        """导出当前状态（须可 JSON 序列化）。"""
        ...

    def load(self, data: Any) -> None:
        """按存档内容恢复状态；data 为 None（旧档无此段）时应静默跳过。"""
        ...


class CharacterStateCodec:
    """角色运行时状态（情绪 / 对话欲 / 出戏计数）的持久化编解码。

    这些字段会喂给 Responder 的语气与主动发言判断，重启归零等于「性格记忆」断档。
    恢复时**校验角色名**：换了角色却复用同一 session_id 时，拒绝把旧角色的状态套上来。
    """

    key = "character_state"

    def __init__(self, character) -> None:
        """初始化。

        Args:
            character: Character（鸭子类型：需有 name / status）
        """
        self._logger = LoggerManager.get_logger("PERSIST")
        self._character = character

    def dump(self) -> dict:
        """导出角色状态。

        Returns:
            dict: 角色名 + 情绪 + 对话欲 + 扩展状态
        """
        status = self._character.status
        return {
            "name": self._character.name,
            "emotion": status.emotion,
            "motivation": status.motivation,
            "extra": dict(status.extra),
        }

    def load(self, data: Any) -> None:
        """恢复角色状态；角色名不匹配时拒绝套用。

        Args:
            data: dump() 产出的字典；None 时跳过
        """
        if not isinstance(data, dict):
            return
        name = str(data.get("name", ""))
        if name and name != self._character.name:
            self._logger.warning(
                f"存档角色为 {name!r}，当前角色是 {self._character.name!r}，跳过角色状态恢复"
            )
            return

        status = self._character.status
        if "emotion" in data:
            status.emotion = str(data["emotion"])
        if "motivation" in data:
            try:
                status.motivation = float(data["motivation"])
            except TypeError, ValueError:
                self._logger.warning(f"对话欲字段非法，跳过: {data.get('motivation')!r}")
        extra = data.get("extra")
        if isinstance(extra, dict):
            status.extra.update(extra)
        self._logger.info(f"角色状态已恢复: 情绪={status.emotion} 对话欲={status.motivation}")


class SessionPersister:
    """会话状态持久化器：脏标记 + 单飞任务合并保存。

    事件（记忆写入 / 回合结束）只置脏标记；若无保存在飞则起一个
    后台任务，等待短暂窗口把同一回合的多次写入合并成一次落盘，
    主链路零阻塞。进程被杀最多丢最近一个合并窗口的状态。
    """

    _MEMORY_KEY = "work_memory"
    """ 工作记忆在文件载荷中的字段名 """

    def __init__(
        self,
        backend: PersistenceBackend,
        session_id: str,
        memory,
        sessions: SessionManager,
        bus: EventBus,
        *,
        character_name: str = "",
        responder_owner: str = "responder",
        coalesce_seconds: float = 1.5,
        codecs: list[StateCodec] | None = None,
    ) -> None:
        """初始化并注册事件订阅。

        Args:
            backend: 持久化后端（可插拔）
            session_id: 会话标识（与 memory 的会话一致）
            memory: MemoryManager（鸭子类型：需有 snapshot()/restore()）
            sessions: SessionManager（恢复 responder 会话历史用）
            bus: 事件总线（订阅 MEMORY_WRITE / TURN_END）
            character_name: 角色名（写入文件元信息）
            responder_owner: 要持久化历史的 owner，默认 "responder"
            coalesce_seconds: 脏标记后的合并等待窗口（秒）
            codecs: 额外状态编解码器（角色状态 / 世界运行时旋钮等），由装配层提供
        """
        self._logger = LoggerManager.get_logger("PERSIST")
        self._backend = backend
        self._session_id = session_id
        self._memory = memory
        self._sessions = sessions
        self._bus = bus
        self._character_name = character_name
        self._responder_owner = responder_owner
        self._coalesce = coalesce_seconds
        self._codecs = list(codecs or [])
        """ 额外状态编解码器；本模块只负责编排，形状由它们决定 """

        self._dirty = asyncio.Event()
        self._saving = False
        self._stopped = False
        self._flushed = False
        """ 是否已写过最终快照（保证 flush 幂等）"""
        self._tasks = TaskRegistry("PERSIST")
        """ 后台保存在飞任务的强引用登记处 —— 没有它，`_save_loop` 若被 GC 回收，
        `_saving` 就永远停在 True，此后所有落盘静默失效 """
        self.turn_count = 0
        """ 已持久化的回合数（从恢复文件续计）"""

        self._key = f"sessions/{session_id}/session"
        bus.subscribe(EventTopic.MEMORY_WRITE, self._on_dirty_event)
        bus.subscribe(EventTopic.TURN_END, self._on_turn_end)

    async def _on_dirty_event(self, event) -> None:
        """MEMORY_WRITE -> 置脏并确保有保存任务在飞"""
        self._mark_dirty()

    async def _on_turn_end(self, event) -> None:
        """TURN_END -> 记回合数、置脏"""
        payload = event.payload
        self.turn_count = getattr(payload, "turn", self.turn_count + 1)
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        """置脏；无在飞保存时启动单飞任务"""
        if self._stopped:
            return
        self._dirty.set()
        if not self._saving:
            self._saving = True
            self._tasks.spawn(self._save_loop(), name="persist-save")

    async def _save_loop(self) -> None:
        """单飞保存循环：窗口期内的新脏标记合并进同一次落盘"""
        try:
            while self._dirty.is_set():
                self._dirty.clear()
                await asyncio.sleep(self._coalesce)
                await self._write_snapshot()
        except Exception:
            self._logger.exception("会话持久化失败（不影响主链路）")
        finally:
            self._saving = False
            # 窗口期内又有写入（且未被 flush 接管）则补一轮
            if self._dirty.is_set() and not self._stopped:
                self._mark_dirty()

    async def _write_snapshot(self) -> None:
        """组装当前状态快照并交给后端落盘"""
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": self._session_id,
            "character_name": self._character_name,
            "turn_count": self.turn_count,
            "saved_at": time.time(),
            "work_memory": [msgspec.to_builtins(m) for m in self._memory.snapshot()],
            "responder_messages": [
                msgspec.to_builtins(m)
                for m in self._sessions.export_messages(self._responder_owner)
            ],
        }
        for codec in self._codecs:
            payload[codec.key] = codec.dump()
        await self._backend.save(self._key, payload)
        self._logger.debug(
            f"会话快照已落盘: turn={self.turn_count} "
            f"记忆={len(payload['work_memory'])}条 消息={len(payload['responder_messages'])}条 "
            f"附加段={[c.key for c in self._codecs]}"
        )

    async def restore(self) -> bool:
        """启动时从后端恢复状态。

        Returns:
            bool: True 表示成功恢复（调用方可据此跳过开场白）；
                无存档或文件无法恢复时返回 False
        """
        data = await self._backend.load(self._key)
        if not data:
            self._logger.info(f"无会话存档，全新开始: {self._session_id}")
            return False

        try:
            turn_count = int(data.get("turn_count", 0))
            work_items = [msgspec.convert(m, MemoryItem) for m in data.get("work_memory", [])]
            messages = [msgspec.convert(m, Message) for m in data.get("responder_messages", [])]
        except Exception:
            self._logger.exception("会话存档字段解析失败，按无存档处理")
            return False

        self.turn_count = turn_count
        if work_items:
            self._memory.restore(work_items)
        if messages:
            self._sessions.import_messages(self._responder_owner, messages)
        self._restore_codecs(data)
        self._logger.info(
            f"会话已恢复: {self._session_id} turn={turn_count} "
            f"记忆={len(work_items)}条 消息={len(messages)}条 "
            f"附加段={[c.key for c in self._codecs]}"
        )
        return True

    def _restore_codecs(self, data: dict) -> None:
        """逐个恢复附加状态；单个 codec 失败不影响其他与主流程。

        Args:
            data: 会话存档载荷
        """
        for codec in self._codecs:
            try:
                codec.load(data.get(codec.key))
            except Exception:
                self._logger.exception(f"附加状态恢复失败（跳过）: {codec.key}")

    async def flush(self) -> None:
        """立即同步落盘最终快照（优雅关闭用），并停止事件触发的保存。

        **幂等**：重复调用是空操作。关闭流程的顺序是「先写快照、再清空会话」，
        若之后再次 flush，就会拿**已清空**的状态覆盖掉刚写好的好存档
        （`responder_messages` 会被写成空数组，历史对话全丢）。

        同理，**在飞的后台保存在这里必须先取消**：`_save_loop` 可能正卡在合并窗口的
        `sleep` 里，醒来后会拿关闭后的状态再写一次，同样会把好存档覆盖掉。
        """
        if self._flushed:
            self._logger.debug("已有最终快照，跳过重复 flush")
            return
        self._flushed = True
        self._stopped = True
        self._dirty.clear()
        # 先置 _stopped 再取消：_save_loop 的 finally 会检查该标志，不会把脏标记再立起来
        cancelled = await self._tasks.cancel_all()
        if cancelled:
            self._logger.debug(f"取消在飞保存任务 {cancelled} 个，改由最终快照接管")
        try:
            await self._write_snapshot()
            self._logger.info("关闭前会话快照已落盘")
        except Exception:
            self._logger.exception("关闭前落盘失败")

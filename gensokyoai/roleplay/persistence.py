"""会话持久化编排 —— 事件驱动的状态落盘与重启恢复。

存什么、何时存归这里，怎么存归 PersistenceBackend（可插拔）。
通信走事件总线：订阅 MEMORY_WRITE / TURN_END 触发保存，
自身不侵入主循环。
"""

import asyncio
import time
from typing import Any

import msgspec

from ..core.event_bus import EventBus
from ..core.persistence import PersistenceBackend
from ..core.session_manager import SessionManager
from ..schemas.event_schema import EventTopic
from ..schemas.memory_schema import MemoryItem
from ..schemas.model_schema import Message
from ..utils.logger import LoggerManager

_SCHEMA_VERSION = 1
""" 会话文件格式版本（后续结构变更时据此迁移）"""


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

        self._dirty = asyncio.Event()
        self._saving = False
        self._stopped = False
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
            asyncio.get_running_loop().create_task(self._save_loop())

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
        await self._backend.save(self._key, payload)
        self._logger.debug(
            f"会话快照已落盘: turn={self.turn_count} "
            f"记忆={len(payload['work_memory'])}条 消息={len(payload['responder_messages'])}条"
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
        self._logger.info(
            f"会话已恢复: {self._session_id} turn={turn_count} "
            f"记忆={len(work_items)}条 消息={len(messages)}条"
        )
        return True

    async def flush(self) -> None:
        """立即同步落盘最终快照（优雅关闭用），并停止事件触发的保存。"""
        self._stopped = True
        self._dirty.clear()
        try:
            await self._write_snapshot()
            self._logger.info("关闭前会话快照已落盘")
        except Exception:
            self._logger.exception("关闭前落盘失败")

"""多房间注册表 —— 一群一房，按需创建（v1 hub 的瘦身版：先不做空闲回收）。"""

import asyncio
from collections.abc import Callable

from .loop import Loop
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("HUB")


class Hub:
    """room_id -> Loop，创建时启动其循环任务。"""

    def __init__(self, factory: Callable[[str], Loop]) -> None:
        """初始化。

        Args:
            factory: 由 room_id 造一个完整 Loop（组装器官是调用方的事）
        """
        self._factory = factory
        self._loops: dict[str, Loop] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._closing = False

    def get(self, room_id: str) -> Loop:
        """取房间循环；不存在则创建并启动，任务已退出则重启。"""
        loop = self._loops.get(room_id)
        if loop is None:
            loop = self._factory(room_id)
            self._loops[room_id] = loop
            self._start(room_id, loop)
            return loop
        task = self._tasks.get(room_id)
        if task is not None and task.done() and not self._closing:
            # 被取消也算死了：消息还在进房，任务却不在了，房间会永远不回声
            _logger.warning(f"房间 {room_id} 的循环任务已退出，重启")
            self._start(room_id, loop)
        return loop

    def peek(self, room_id: str) -> Loop | None:
        """只看不建：房间存在返回 Loop，否则 None。"""
        return self._loops.get(room_id)

    def has(self, room_id: str) -> bool:
        """房间是否已存在。"""
        return room_id in self._loops

    def room_ids(self) -> list[str]:
        """当前活跃房间。"""
        return list(self._loops)

    def _start(self, room_id: str, loop: Loop) -> None:
        """起一个房间循环任务，并盯着它——循环悄悄死掉会让房间永远不回话。"""
        task = asyncio.create_task(loop.run(), name=f"loop:{room_id}")

        def _done(done: asyncio.Task, rid: str = room_id) -> None:
            self._on_done(rid, done)

        task.add_done_callback(_done)
        self._tasks[room_id] = task

    def _on_done(self, room_id: str, task: asyncio.Task) -> None:
        """任务结束时把原因说清楚（正常退出、异常、被取消）。"""
        if task.cancelled():
            _logger.debug(f"房间 {room_id} 的循环任务被取消")
            return
        error = task.exception()
        if error is not None:
            _logger.error(f"房间 {room_id} 的循环任务异常退出: {error!r}")
            return
        _logger.warning(f"房间 {room_id} 的循环任务自己退出了（下条消息会重启）")

    async def shutdown(self) -> None:
        """停掉全部循环任务（进程退出时调）。"""
        self._closing = True
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._loops.clear()  # 否则 shutdown 后 get() 会拿到没有任务在跑的房间

"""后台任务登记处（BgTasks）—— 一次性任务与周期任务，统一监督与收尾。

持有任务强引用；异常记入日志和 health。一次性任务退出前先收尾，周期任务直接取消。
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .health import HealthCenter
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("BGTASKS")

_DRAIN_TIMEOUT_S = 5.0
""" 退出时等一次性任务收尾的上限（秒） """


class BgTasks:
    """后台任务登记处（一个进程一个，入口持有，退出时 shutdown）。"""

    def __init__(self, health: HealthCenter | None = None) -> None:
        """初始化。

        Args:
            health: 健康中心（None = 异常只记日志不计数）
        """
        self._health = health
        self._tasks: set[asyncio.Task] = set()
        self._oneshot: set[asyncio.Task] = set()
        """ 一次性任务（退出时要等它们写完） """
        self._closers: list[Callable[[], Awaitable[None]]] = []
        """ 退出时关闭的资源（任务收尾之后调用） """

    def spawn(self, name: str, coro: Coroutine[Any, Any, object]) -> asyncio.Task:
        """起一个一次性任务；异常记日志和 health，不影响其它任务。

        Returns:
            asyncio.Task: 任务句柄（调用方想等结果可以 await 它）
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        self._oneshot.add(task)
        task.add_done_callback(self._reap)
        return task

    def every(
        self, name: str, interval: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        """登记周期任务：每 interval 秒一轮，本轮失败不影响下一轮。"""
        task = asyncio.create_task(self._loop(name, interval, fn), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._reap)

    def on_shutdown(self, closer: Callable[[], Awaitable[None]]) -> None:
        """登记退出时要关的资源（在任务收尾之后调用）。"""
        self._closers.append(closer)

    async def _loop(
        self, name: str, interval: float, fn: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(f"周期任务 {name} 本轮失败")
                self._count(name)

    def _reap(self, task: asyncio.Task) -> None:
        """任务收尾：摘登记，异常记账。"""
        self._tasks.discard(task)
        self._oneshot.discard(task)
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            _logger.opt(exception=exc).error(f"后台任务 {task.get_name()} 失败")
            self._count(task.get_name())

    def _count(self, name: str) -> None:
        if self._health is not None:
            self._health.count(f"bgtasks.{name}.errors")

    async def idle(self) -> None:
        """等一次性任务收尾（周期任务不等）。"""
        if self._oneshot:
            await asyncio.gather(*list(self._oneshot), return_exceptions=True)

    async def shutdown(self, *, drain: float = _DRAIN_TIMEOUT_S) -> None:
        """等一次性任务收尾（最多 drain 秒），再取消全部任务，最后关资源。"""
        if self._oneshot:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*list(self._oneshot), return_exceptions=True),
                    drain,
                )
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self._oneshot.clear()
        for closer in self._closers:
            try:
                await closer()
            except Exception:
                _logger.exception("退出清理失败")
        self._closers.clear()

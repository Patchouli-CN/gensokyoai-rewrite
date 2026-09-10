"""后台任务管理器 —— 让 fire-and-forget 任务持有一份强引用。

**为什么需要它**：`asyncio` 的事件循环对任务只持**弱引用**。`asyncio.create_task()`
的返回值若无人接住，任务可能在执行途中被垃圾回收 —— CPython 上的表现是
「跑一半没了」「`finally` 没执行」「`await` 之后的代码永不运行」，而且**一点报错都没有**
（官方文档对此有明确警告：*Save a reference to the result of this function, to avoid
a task disappearing mid-execution.*）。

本项目的后果尤其严重：`SessionPersister` 的 `_saving` 标志在任务 `finally` 里复位，
任务若被回收，标志永远停在 `True`，此后**所有落盘静默失效**。

因此凡是「不想 await、丢到后台去跑」的协程，一律走 `TaskManager.spawn()`：

- 登记强引用，任务结束自动摘除（不会无限增长）
- 异常统一记录，避免 `Task exception was never retrieved` 这类只有 GC 时才冒出来的告警
- 关闭时可 `cancel_all()` / `drain()`，保证在途任务收尾而不是凭空消失
"""

import asyncio
import time
from collections.abc import Coroutine
from typing import Any

from .logger import LoggerManager


class TaskManager:
    """后台任务管理器：持强引用 + 统一异常记录 + 批量收尾。

    典型用法::

        tasks = TaskManager("WORLD")
        tasks.spawn(self._distill())          # 不必接返回值
        await tasks.drain(timeout=2.0)        # 关闭时等在途任务收尾
    """

    def __init__(self, label: str = "BG") -> None:
        """初始化。

        Args:
            label: 日志名与任务名前缀（便于 `asyncio.all_tasks()` 里辨认归属）
        """
        self._label = label
        self._logger = LoggerManager.get_logger(label)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._counter = 0

    def spawn(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        """把协程丢到后台执行，并在本管理器登记强引用。

        Args:
            coro: 待执行协程
            name: 任务名；None 时用 `<label>-<序号>`

        Returns:
            asyncio.Task: 任务句柄（调用方可以不接 —— 管理器已持引用）

        Raises:
            RuntimeError: 当前没有运行中的事件循环
        """
        self._counter += 1
        task = asyncio.create_task(coro, name=name or f"{self._label}-{self._counter}")
        self._tasks.add(task)
        # done 回调同时负责「摘除引用」与「取走异常」，两者都不能省：
        # 前者防止集合无限增长，后者防止异常退化成无人认领的告警
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[Any]) -> None:
        """任务结束：摘除引用；非取消类异常记录下来。

        Args:
            task: 已结束的任务
        """
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            # 协程自身抛出 CancelledError（而非被 cancel()）时会走到这里
            return
        if exc is not None:
            self._logger.opt(exception=exc).error(f"后台任务失败: {task.get_name()}")

    @property
    def pending(self) -> int:
        """在途任务数"""
        return len(self._tasks)

    def names(self) -> list[str]:
        """在途任务名列表（排查“谁还在跑”用）。

        Returns:
            list[str]: 任务名列表
        """
        return sorted(t.get_name() for t in self._tasks)

    async def drain(self, timeout: float | None = None) -> int:
        """等待在途任务自然结束。

        循环等待而非一次性 `wait`：在途任务自己也可能再 `spawn` 新任务
        （例如蒸馏写记忆 -> 淘汰转存），一轮等不完。

        Args:
            timeout: 最长等待秒数（整体预算）；None 表示一直等。
                超时后剩余任务会被取消（否则关闭流程会被慢侧链拖死）

        Returns:
            int: 等到结束的任务数
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        finished = 0
        while self._tasks:
            if deadline is None:
                budget: float | None = None
            else:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    self._logger.warning(
                        f"后台任务超时未结束（{len(self._tasks)} 个），取消之: {self.names()}"
                    )
                    await self.cancel_all()
                    break
            done, _ = await asyncio.wait(set(self._tasks), timeout=budget)
            finished += len(done)
        return finished

    async def cancel_all(self) -> int:
        """取消所有在途任务并等它们收尾（`finally` 会正常执行）。

        Returns:
            int: 被取消的任务数
        """
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        return len(pending)

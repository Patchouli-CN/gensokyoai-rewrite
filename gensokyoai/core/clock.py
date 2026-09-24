"""生物钟 —— 世界内的定时任务调度（cron 语义的极简实现）。

不引调度库的理由：任务只有个位数、精度秒级就够、生命周期跟着世界走
（世界关了钟就停）。while+sleep 模式对齐 `_initiative_loop` 的既有实践。

- `every(name, interval_s, fn)`：周期任务（主动发言、将来的定时蒸馏/睡眠巩固）
- `once(name, delay_s, fn)`：一次性任务（老项目 reminders 的 at 语义）
- 单次任务异常进日志，不影响其他任务与心跳（任务隔离）
"""

import asyncio
from collections.abc import Awaitable, Callable

from ..utils.logger import LoggerManager

type _Job = Callable[[], Awaitable[None]]


class BiologicalClock:
    """生物钟：注册任务 -> start 启动心跳 -> stop 随世界关闭。"""

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("CLOCK")
        self._jobs: dict[str, tuple[float, _Job, bool]] = {}
        """ name -> (间隔秒, 任务, 是否周期) """
        self._running: dict[str, asyncio.Task] = {}

    def every(self, name: str, interval_s: float, job: _Job) -> None:
        """注册周期任务：每隔 interval_s 秒跳一次。

        Args:
            name: 任务名（重复注册报错，防覆盖）
            interval_s: 间隔秒数
            job: 异步任务体；要不要跳过一次由任务自己判断（如忙碌/关闭中）
        """
        self._register(name, interval_s, job, recurring=True)

    def once(self, name: str, delay_s: float, job: _Job) -> None:
        """注册一次性任务：delay_s 秒后执行一次（at 语义的相对时间版）。

        Args:
            name: 任务名
            delay_s: 延迟秒数
            job: 异步任务体
        """
        self._register(name, delay_s, job, recurring=False)

    def cancel(self, name: str) -> bool:
        """注销任务（未启动的移除注册，运行中的取消）。

        Returns:
            bool: 是否存在该任务
        """
        existed = self._jobs.pop(name, None) is not None
        task = self._running.pop(name, None)
        if task is not None:
            task.cancel()
            existed = True
        return existed

    def _register(self, name: str, interval_s: float, job: _Job, *, recurring: bool) -> None:
        """注册校验：重名报错（防静默覆盖），间隔不能为负"""
        if name in self._jobs:
            raise ValueError(f"生物钟任务重复注册: {name}")
        if interval_s < 0:
            raise ValueError(f"间隔不能为负: {interval_s}")
        self._jobs[name] = (interval_s, job, recurring)

    @property
    def names(self) -> list[str]:
        """已注册任务名"""
        return list(self._jobs)

    async def start(self) -> None:
        """启动所有已注册任务的心跳"""
        for name, (interval, job, recurring) in self._jobs.items():
            if name in self._running:
                continue
            runner = self._run_recurring if recurring else self._run_once
            self._running[name] = asyncio.create_task(runner(name, interval, job))
        if self._jobs:
            self._logger.info(f"生物钟启动: {self.names}")

    async def stop(self, timeout: float = 2.0) -> None:
        """停止全部任务（取消 + 限时等待收尾）。"""
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                self._logger.warning(f"生物钟任务未在 {timeout}s 内收尾: {task.get_name()}")
        self._running.clear()

    async def _run_recurring(self, name: str, interval: float, job: _Job) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await job()
            except Exception:
                self._logger.exception(f"生物钟任务失败（不影响心跳）: {name}")

    async def _run_once(self, name: str, delay: float, job: _Job) -> None:
        try:
            await asyncio.sleep(delay)
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(f"生物钟任务失败: {name}")
        finally:
            self._jobs.pop(name, None)
            self._running.pop(name, None)


__all__ = ["BiologicalClock"]

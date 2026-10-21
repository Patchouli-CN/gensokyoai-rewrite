"""静息期整理（DMN）—— 房间安静下来后跑整理任务：记忆压缩 / OOC 反刍 / 日结。

默认模式网络梗：清醒期热路径干干净净，所有反刍都挪到安静时段。
判定很简单：房间静默超过阈值 = 睡着了，可以整理。全局时段（深夜）以后
要加也是一行条件——先不做。

keeper 只管判定，巡检节奏由 BgTasks.every 给。
"""

import time
from collections.abc import Awaitable, Callable

from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("DMN")


class DmnKeeper:
    """静息期调度器：盯住各房间的活跃度，安静了就跑它们的整理任务。"""

    def __init__(
        self,
        *,
        idle_s: float = 1800.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化。

        Args:
            idle_s: 房间静默多久视为入睡（秒）
            clock: 时钟（测试注入）
        """
        self._idle_s = idle_s
        self._clock = clock
        self._wards: dict[
            str, tuple[Callable[[], float], Callable[[], Awaitable[None]]]
        ] = {}
        self._done_at: dict[str, float] = {}

    def watch(
        self,
        room_name: str,
        last_activity: Callable[[], float],
        task: Callable[[], Awaitable[None]],
    ) -> None:
        """登记一个房间的整理任务（入睡即执行一次，醒来后再睡可再执行）。

        Args:
            room_name: 房间名
            last_activity: 返回房间最后活跃时间戳
            task: 整理任务（记忆压缩 / OOC 反刍 / 日结打包成一个 async  callable）
        """
        self._wards[room_name] = (last_activity, task)

    async def patrol(self) -> None:
        """巡检一轮：入睡且这一觉还没整理过的房间，各跑一次整理。"""
        now = self._clock()
        for name, (last_activity, task) in self._wards.items():
            if now - last_activity() < self._idle_s:
                continue  # 还醒着
            if self._done_at.get(name, 0.0) >= last_activity():
                continue  # 这一觉已经整理过了
            try:
                await task()
                self._done_at[name] = now
            except Exception:
                # 整理失败不炸巡检，下轮再试（不写 done_at）
                _logger.exception(f"房间 {name} 静息整理失败")

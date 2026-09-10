"""队列感知器 —— 多路输入的合流点。

外部（WS 连接、频道入口）把标准化快照推进队列，world 从队列拉取。
多个连接推同一个队列，就实现了「多路复用」：N 路输入合流成一条快照流，
而 `SceneSnapshot.sender` 天然区分是谁说的 —— 不需要给 Perceiver 加路由参数。
"""

import asyncio

from ..schemas.scene_schema import SceneSnapshot
from .base import Perceiver


class QueuePerceiver(Perceiver):
    """队列式感知器：`push` 入队，`next_snapshot` 阻塞取。

    等待时与停止请求赛跑，保证 `request_stop()` 后能及时退出主循环。
    """

    def __init__(
        self, queue: asyncio.Queue[SceneSnapshot] | None = None, *, maxsize: int = 0
    ) -> None:
        """初始化。

        Args:
            queue: 外部提供的队列；None 时自建
            maxsize: 自建队列的容量上限（0 表示不限）；队列满时新输入被丢弃并告警
        """
        super().__init__()
        self.queue: asyncio.Queue[SceneSnapshot] = queue or asyncio.Queue(maxsize)
        self._stop = asyncio.Event()

    def push(self, snapshot: SceneSnapshot) -> None:
        """推入一条快照（非阻塞）。

        Args:
            snapshot: 标准化场景快照
        """
        try:
            self.queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            self._logger.warning("快照队列已满，丢弃本次输入（背压保护）")

    def request_stop(self) -> None:
        """请求停止：唤醒阻塞中的 `next_snapshot`。"""
        super().request_stop()
        self._stop.set()

    async def close(self) -> None:
        """关闭感知器（等同 request_stop，用于世界回收）。"""
        self.request_stop()

    async def next_snapshot(self) -> SceneSnapshot | None:
        """阻塞取一条快照；收到停止请求时返回 None。

        Returns:
            SceneSnapshot | None: 取到的快照；None 表示已请求停止
        """
        if self._stop_requested:
            return None

        get_task = asyncio.create_task(self.queue.get())
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({get_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if get_task.done() and not get_task.cancelled():
                return get_task.result()
            return None
        finally:
            for task in (get_task, stop_task):
                if not task.done():
                    task.cancel()
            # 等被取消的任务真正结束，避免 pending task 告警
            await asyncio.gather(get_task, stop_task, return_exceptions=True)

"""队列感知器 —— 多路输入的合流点。

外部（WS 连接、频道入口）把标准化快照推进队列，world 从队列拉取。
多个连接推同一个队列，就实现了「多路复用」：N 路输入合流成一条快照流，
而 `SceneSnapshot.sender` 天然区分是谁说的 —— 不需要给 Perceiver 加路由参数。

**合并窗口**（merge_window > 0 时）：首条消息开启一个固定时窗，窗内到达的
消息攒成一批，到点合并成**一个**快照进队。治两种群聊病：

- **话没说完**：用户一句话分好几条发，攒齐了再给角色看
- **多人同时 @**：一轮思考、一条回复回应所有人，而不是逐条回 N 条刷屏
  （还省 N-1 轮思考链的钱）

合并语义见 `merge_snapshots`。
"""

import asyncio
from collections.abc import Sequence

from ..schemas.scene_schema import SceneEvent, SceneSnapshot
from .base import Perceiver


def merge_snapshots(snaps: Sequence[SceneSnapshot]) -> SceneSnapshot:
    """把一批快照合并成一个（单条原样返回）。

    - 同一发送者：内容按行拼接，sender 不变（「话没说完」情形）
    - 多个发送者：每行自带 `发送者: 内容` 前缀（发言归属不丢），
      sender 取「最后被 direct 的人」（回复主要回应他），否则取末位发送者
    - is_direct 取或（任何一条针对角色，整批都算）
    - participants 为去重后的发送者列表；每条原始消息进 event_queue 留痕
    - context_snippet 取最后一条的（最新鲜）；timestamp 取最后一条的
    """
    if len(snaps) == 1:
        return snaps[0]

    senders = [s.sender for s in snaps]
    unique = list(dict.fromkeys(senders))
    primary = next((s.sender for s in reversed(snaps) if s.is_direct), senders[-1])
    if len(unique) == 1:
        content = "\n".join(s.content for s in snaps if s.content)
        sender = unique[0]
    else:
        content = "\n".join(f"{s.sender}: {s.content}" for s in snaps if s.content)
        sender = primary

    return SceneSnapshot(
        scene_type=snaps[-1].scene_type,
        sender=sender,
        content=content,
        is_direct=any(s.is_direct for s in snaps),
        context_snippet=snaps[-1].context_snippet,
        participants=unique,
        event_queue=[
            SceneEvent(kind="message", sender=s.sender, content=s.content, timestamp=s.timestamp)
            for s in snaps
        ],
        timestamp=snaps[-1].timestamp,
    )


class QueuePerceiver(Perceiver):
    """队列式感知器：`push` 入队，`next_snapshot` 阻塞取。

    等待时与停止请求赛跑，保证 `request_stop()` 后能及时退出主循环。
    """

    def __init__(
        self,
        queue: asyncio.Queue[SceneSnapshot] | None = None,
        *,
        maxsize: int = 0,
        merge_window: float = 0.0,
    ) -> None:
        """初始化。

        Args:
            queue: 外部提供的队列；None 时自建
            maxsize: 自建队列的容量上限（0 表示不限）；队列满时新输入被丢弃并告警
            merge_window: 合并窗口秒数；>0 时窗内消息攒批合并成一条快照
                （见模块 docstring），0 = 逐条直接进队（旧行为）
        """
        super().__init__()
        self.queue: asyncio.Queue[SceneSnapshot] = queue or asyncio.Queue(maxsize)
        self._merge_window = merge_window
        self._pending: list[SceneSnapshot] = []
        self._flush_handle: asyncio.TimerHandle | None = None
        self._stop = asyncio.Event()

    def push(self, snapshot: SceneSnapshot) -> None:
        """推入一条快照（非阻塞）；开启合并窗口时进待合并批次。

        Args:
            snapshot: 标准化场景快照
        """
        if self._merge_window <= 0:
            self._put(snapshot)
            return
        self._pending.append(snapshot)
        if self._flush_handle is None:
            # 固定时窗从首条起算（非滑动）：持续刷屏不会无限推迟世界的回合
            self._flush_handle = asyncio.get_running_loop().call_later(
                self._merge_window, self._flush_pending
            )

    def _put(self, snapshot: SceneSnapshot) -> None:
        try:
            self.queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            self._logger.warning("快照队列已满，丢弃本次输入（背压保护）")

    def _flush_pending(self) -> None:
        """合并窗口到点：攒批合并成一条快照进队。"""
        self._flush_handle = None
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        merged = merge_snapshots(batch)
        if len(batch) > 1:
            self._logger.info(f"合并窗口聚合 {len(batch)} 条消息: {merged.participants}")
        self._put(merged)

    def request_stop(self) -> None:
        """请求停止：唤醒阻塞中的 `next_snapshot`，丢弃未合并的批次。"""
        super().request_stop()
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._pending = []
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

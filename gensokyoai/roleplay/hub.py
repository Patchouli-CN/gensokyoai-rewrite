"""频道中枢 —— 多路复用 + 世界注册表 + 空闲回收。

语义：**一个频道 = 一个场景**（一个角色、一个会话）。
多个连接挂到同一频道时共享同一个 `TouhouWorld`；连接断开只是退订，
频道空闲超过 `idle_ttl` 才回收世界（回收时走世界自身的优雅关闭，落盘记忆与会话）。

多路复用的两个接合点：
- 输入：所有连接的快照推进**同一条** `QueuePerceiver` 队列（N 路合流）
- 输出：世界用 `BroadcastMouth` 把每句话广播给该频道**所有**在线连接
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.resource import tenant_scope
from ..core.session_manager import SessionManager
from ..eyes.queue import QueuePerceiver
from ..mouth.broadcast import BroadcastMouth, DeliverSink
from ..schemas.scene_schema import SceneSnapshot, SceneType
from ..utils.logger import LoggerManager
from .loop import TouhouWorld

if TYPE_CHECKING:
    from .character import Character

WorldFactory = Callable[[str, QueuePerceiver, BroadcastMouth], TouhouWorld]
""" 由 channel_id + 感知器 + 口层 造一个世界的工厂 """

_DEFAULT_STOP_TIMEOUT = 5.0
""" 回收时等世界优雅关闭的秒数，超时才强杀 """


@dataclass
class Channel:
    """一个频道 = 一个世界 + 一条合流队列 + 一个广播口。"""

    channel_id: str
    world: TouhouWorld
    perceiver: QueuePerceiver
    mouth: BroadcastMouth
    task: asyncio.Task
    launched_at: float
    last_active: float


class ChannelHub:
    """频道中枢：`channel_id -> Channel`，负责挂载 / 提交 / 回收。

    租户归属：每个频道的世界任务包在 `tenant_scope(channel_id)` 里，
    于是该频道内的所有模型调用自动计到以 channel_id 为名的租户配额上。
    """

    def __init__(
        self,
        *,
        sessions: SessionManager,
        character: Character,
        world_factory: WorldFactory | None = None,
        storage_dir: str = "data",
        idle_ttl: float = 600.0,
        stop_timeout: float = _DEFAULT_STOP_TIMEOUT,
        world_kwargs: dict | None = None,
        clock=time.monotonic,
    ) -> None:
        """初始化。

        Args:
            sessions: 多模型路由的会话管理器（所有频道共享）
            character: 角色（所有频道共用同一个角色卡）
            world_factory: 世界的构造工厂；None 时用默认（TouhouWorld + 频道隔离持久化）
            storage_dir: 默认工厂使用的持久化根目录
            idle_ttl: 无订阅者后回收世界的空闲秒数
            stop_timeout: 回收时等世界优雅关闭的秒数
            world_kwargs: 透传给默认工厂的 TouhouWorld 额外参数（如 OOC / 过渡语旋钮）
            clock: 时钟函数（可注入以便测试）
        """
        self._logger = LoggerManager.get_logger("HUB")
        self._sessions = sessions
        self._character = character
        self._storage_dir = storage_dir
        self._idle_ttl = idle_ttl
        self._stop_timeout = stop_timeout
        self._world_kwargs = dict(world_kwargs or {})
        self._clock = clock
        self._channels: dict[str, Channel] = {}
        self._factory: WorldFactory = world_factory or self._default_world

    def _default_world(
        self, channel_id: str, perceiver: QueuePerceiver, mouth: BroadcastMouth
    ) -> TouhouWorld:
        """默认世界工厂：每频道一个会话 ID，持久化天然隔离。"""
        return TouhouWorld(
            eye=perceiver,
            mouth=mouth,
            sessions=self._sessions,
            character=self._character,
            session_id=channel_id,
            storage_dir=self._storage_dir,
            **self._world_kwargs,
        )

    # ---------------------------------------------------------------- 挂载

    def attach(self, channel_id: str, sink: DeliverSink) -> Channel:
        """把一个连接（订阅者）挂到频道；频道不存在则创建（含世界启动）。

        Args:
            channel_id: 频道标识
            sink: 可接收投递帧的订阅者

        Returns:
            Channel: 该频道的运行时对象
        """
        channel = self._get_or_create(channel_id)
        channel.mouth.attach(sink)
        channel.last_active = self._clock()
        self._logger.info(f"连接加入频道: {channel_id}（在线 {channel.mouth.subscriber_count}）")
        return channel

    def detach(self, channel_id: str, sink: DeliverSink) -> None:
        """把一个连接从频道摘除（不立刻销毁世界，交给空闲回收）。

        Args:
            channel_id: 频道标识
            sink: 订阅者
        """
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        channel.mouth.detach(sink)
        channel.last_active = self._clock()
        self._logger.info(f"连接离开频道: {channel_id}（在线 {channel.mouth.subscriber_count}）")

    # ---------------------------------------------------------------- 输入

    def submit(
        self,
        channel_id: str,
        *,
        user: str,
        text: str,
        scene_type: SceneType = "group_chat",
        is_direct: bool = False,
        context_snippet: list[str] | None = None,
    ) -> None:
        """把一条用户输入提交给频道（包成快照入队，非阻塞）。

        Args:
            channel_id: 频道标识
            user: 发送者
            text: 内容
            scene_type: 场景类型
            is_direct: 是否直接针对角色
            context_snippet: 最近上下文（旧 -> 新）
        """
        channel = self._get_or_create(channel_id)
        snapshot = SceneSnapshot(
            scene_type=scene_type,
            sender=user,
            content=text,
            is_direct=is_direct,
            context_snippet=list(context_snippet or []),
            timestamp=self._clock(),
        )
        channel.perceiver.push(snapshot)
        channel.last_active = self._clock()

    # ---------------------------------------------------------------- 回收

    async def reap_idle(self) -> list[str]:
        """回收空闲频道（无订阅者且静默超时）的世界。

        Returns:
            list[str]: 被回收的频道 ID
        """
        now = self._clock()
        reaped: list[str] = []
        for channel_id, channel in list(self._channels.items()):
            if channel.mouth.subscriber_count > 0:
                continue
            if now - channel.last_active < self._idle_ttl:
                continue
            await self._stop_channel(channel_id)
            reaped.append(channel_id)
        if reaped:
            self._logger.info(f"空闲回收频道: {reaped}")
        return reaped

    async def _stop_channel(self, channel_id: str) -> None:
        """优雅停掉一个频道的世界：先请求停止，超时才强杀。"""
        channel = self._channels.pop(channel_id, None)
        if channel is None:
            return
        channel.perceiver.request_stop()
        _, pending = await asyncio.wait({channel.task}, timeout=self._stop_timeout)
        if pending:
            self._logger.warning(f"频道 {channel_id} 未在 {self._stop_timeout}s 内退出，强制取消")
            channel.task.cancel()
            await asyncio.gather(channel.task, return_exceptions=True)

    async def shutdown(self) -> None:
        """停掉全部频道（进程退出时调用）。"""
        for channel_id in list(self._channels):
            await self._stop_channel(channel_id)

    # ---------------------------------------------------------------- 查询

    def channel_ids(self) -> list[str]:
        """当前活跃频道 ID 列表。"""
        return list(self._channels)

    def online_count(self, channel_id: str) -> int:
        """某频道的在线连接数。"""
        channel = self._channels.get(channel_id)
        return channel.mouth.subscriber_count if channel else 0

    def _get_or_create(self, channel_id: str) -> Channel:
        """取频道；不存在则建世界并启动其主循环。"""
        channel = self._channels.get(channel_id)
        if channel is not None:
            return channel

        perceiver = QueuePerceiver()
        mouth = BroadcastMouth()
        world = self._factory(channel_id, perceiver, mouth)
        now = self._clock()
        task = asyncio.create_task(self._run_world(channel_id, world))
        channel = Channel(
            channel_id=channel_id,
            world=world,
            perceiver=perceiver,
            mouth=mouth,
            task=task,
            launched_at=now,
            last_active=now,
        )
        self._channels[channel_id] = channel
        self._logger.info(f"创建频道: {channel_id}")
        return channel

    async def _run_world(self, channel_id: str, world: TouhouWorld) -> None:
        """频道世界的主循环：包在租户作用域里跑（配额据此记账）。"""
        with tenant_scope(channel_id):
            try:
                await world.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(f"频道 {channel_id} 的世界异常退出")
            finally:
                self._channels.pop(channel_id, None)
                self._logger.info(f"频道世界已退出: {channel_id}")

    def __repr__(self) -> str:
        return f"ChannelHub(channels={self.channel_ids()})"


__all__ = ["Channel", "ChannelHub", "WorldFactory"]

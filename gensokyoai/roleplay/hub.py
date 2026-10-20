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

from ..core.config import WorldSettings
from ..core.persistence import JsonFilePersistence
from ..core.resource import tenant_scope
from ..core.session_manager import SessionManager
from ..mouth.broadcast import BroadcastMouth, DeliverSink
from ..satori.queue import QueuePerceiver
from ..schemas.scene_schema import SceneSnapshot, SceneType
from ..utils.logger import LoggerManager
from .loop import TouhouWorld

if TYPE_CHECKING:
    from .character import Character

type WorldFactory = Callable[[str, QueuePerceiver, BroadcastMouth], TouhouWorld]
""" 由 channel_id + 感知器 + 口层 造一个世界的工厂 """

_DEFAULT_STOP_TIMEOUT = 5.0
""" 回收时等世界优雅关闭的秒数，超时才强杀 """

_DEFAULT_MAX_CHANNELS = 32
""" 同时存活的频道数上限（防刷随机频道名耗尽资源） """


class ChannelLimitError(RuntimeError):
    """频道数到达上限，拒绝新建"""


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
        world_settings: WorldSettings | None = None,
        clock=time.monotonic,
        max_channels: int = _DEFAULT_MAX_CHANNELS,
        merge_window: float = 0.0,
    ) -> None:
        """初始化。

        Args:
            sessions: 多模型路由的会话管理器（默认工厂给每个频道派生 scoped 视图）
            character: 角色（所有频道共用同一个角色卡，运行时状态每频道 fork 一份）
            world_factory: 世界的构造工厂；None 时用默认（TouhouWorld + 频道隔离持久化）
            storage_dir: 默认工厂使用的持久化根目录
            idle_ttl: 无订阅者后回收世界的空闲秒数
            stop_timeout: 回收时等世界优雅关闭的秒数
            world_kwargs: 透传给默认工厂的 TouhouWorld 额外参数（协作者：裁判 / 配置）
            world_settings: 世界行为旋钮（WorldSettings；None = 全默认）
            clock: 时钟函数（可注入以便测试）
            max_channels: 同时存活频道数上限，满了再建新频道抛 `ChannelLimitError`
            merge_window: 输入合并窗口秒数（透传 QueuePerceiver；>0 时窗内消息
                攒批合并成一条快照，治「话没说完」与「多人同时 @」）
        """
        self._logger = LoggerManager.get_logger("HUB")
        self._sessions = sessions
        self._character = character
        self._storage_dir = storage_dir
        self._idle_ttl = idle_ttl
        self._stop_timeout = stop_timeout
        self._world_kwargs = dict(world_kwargs or {})
        self._world_settings = world_settings
        self._clock = clock
        self._max_channels = max_channels
        self._merge_window = merge_window
        self._channels: dict[str, Channel] = {}
        self._stopping: dict[str, asyncio.Task] = {}
        """ 正在关闭（落盘中）的频道世界任务：同名新世界启动前先等它收尾 """
        self._orphaned: dict[str, list[DeliverSink]] = {}
        """ 世界意外退出时仍挂着的连接：交接给下一个同名世界，不让回复投进死口层 """
        self._factory: WorldFactory = world_factory or self._default_world
        # 默认工厂下全部频道共享同一个持久化后端：写锁表跨频道去重，
        # 且 prewarm 的暖缓存对所有频道生效（否则每世界一个实例，缓存是摆设）
        self._persistence: JsonFilePersistence | None = None
        if world_factory is None and "persistence" not in self._world_kwargs:
            self._persistence = JsonFilePersistence(storage_dir)

    def _default_world(
        self, channel_id: str, perceiver: QueuePerceiver, mouth: BroadcastMouth
    ) -> TouhouWorld:
        """默认世界工厂：每频道一个会话 ID，持久化天然隔离。

        会话与角色状态也按频道隔离：共享的只有 backend 路由与人设卡——
        共用一个 responder 会话会让 A 群的聊天进 B 群的上下文，且任一
        频道回收时的 reset_all 会清空所有频道的历史与人设前缀。
        """
        kwargs = dict(self._world_kwargs)
        if self._persistence is not None:
            # 共享后端（写锁表 / 暖缓存跨频道生效）；调用方自带后端时尊重之
            kwargs["persistence"] = self._persistence
        return TouhouWorld(
            eye=perceiver,
            mouth=mouth,
            sessions=self._sessions.scoped(channel_id),
            character=self._character.fork(),
            session_id=channel_id,
            storage_dir=self._storage_dir,
            settings=self._world_settings,
            **kwargs,
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
        orphans = self._orphaned.get(channel_id)
        if orphans is not None and sink in orphans:
            orphans.remove(sink)
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

    async def run_reaper(self, interval: float | None = None) -> None:
        """常驻回收节拍：周期调用 `reap_idle()`，直到被取消（服务入口起一个任务跑它）。

        Args:
            interval: 巡检周期秒数；None 时取 idle_ttl 的一半（夹在 1~60 秒）
        """
        period = interval if interval is not None else min(60.0, max(1.0, self._idle_ttl / 2))
        while True:
            await asyncio.sleep(period)
            try:
                await self.reap_idle()
            except Exception:
                self._logger.exception("空闲回收失败（下个周期重试）")

    async def _stop_channel(self, channel_id: str) -> None:
        """优雅停掉一个频道的世界：先请求停止，超时才强杀。"""
        channel = self._channels.pop(channel_id, None)
        if channel is None:
            return
        self._stopping[channel_id] = channel.task
        try:
            channel.perceiver.request_stop()
            _, pending = await asyncio.wait({channel.task}, timeout=self._stop_timeout)
            if pending:
                self._logger.warning(
                    f"频道 {channel_id} 未在 {self._stop_timeout}s 内退出，强制取消"
                )
                channel.task.cancel()
                await asyncio.gather(channel.task, return_exceptions=True)
        finally:
            if self._stopping.get(channel_id) is channel.task:
                del self._stopping[channel_id]

    async def shutdown(self) -> None:
        """停掉全部频道（进程退出时调用；各频道并行收尾）。"""
        await asyncio.gather(*(self._stop_channel(cid) for cid in list(self._channels)))

    # ---------------------------------------------------------------- 查询

    async def prewarm(self, channel_ids: list[str]) -> int:
        """启动期批量预载频道会话存档（如白名单群），首个消息到达时 restore 零 IO。

        仅默认工厂（共享 JSON 后端）下有效；自定义 world_factory 不知道
        会话键的约定，返回 0。已在运行的频道跳过（其会话早已加载）。

        Returns:
            int: 实际预载的频道数
        """
        if self._persistence is None:
            self._logger.warning("自定义 world_factory 下无共享后端，prewarm 无效")
            return 0
        keys = [f"sessions/{cid}/session" for cid in channel_ids if cid not in self._channels]
        count = await self._persistence.prewarm(keys)
        if count:
            self._logger.info(f"会话预载完成: {count} 个频道")
        return count

    @property
    def sessions(self) -> SessionManager:
        """根会话管理器（各频道视图的父级；服务退出时由它统一释放 backend 资源）。"""
        return self._sessions

    def channel_ids(self) -> list[str]:
        """当前活跃频道 ID 列表。"""
        return list(self._channels)

    def worlds(self) -> dict[str, TouhouWorld]:
        """当前活跃频道 -> 世界（只读诊断用，如 /doctor 体检报告）。"""
        return {cid: channel.world for cid, channel in self._channels.items()}

    def online_count(self, channel_id: str) -> int:
        """某频道的在线连接数。"""
        channel = self._channels.get(channel_id)
        return channel.mouth.subscriber_count if channel else 0

    def _get_or_create(self, channel_id: str) -> Channel:
        """取频道；不存在则建世界并启动其主循环（满员抛 `ChannelLimitError`）。"""
        channel = self._channels.get(channel_id)
        if channel is not None:
            return channel

        if len(self._channels) >= self._max_channels:
            raise ChannelLimitError(
                f"频道数到达上限（{self._max_channels}），拒绝新建: {channel_id}"
            )

        perceiver = QueuePerceiver(merge_window=self._merge_window)
        mouth = BroadcastMouth()
        for sink in self._orphaned.pop(channel_id, []):
            mouth.attach(sink)
        world = self._factory(channel_id, perceiver, mouth)
        now = self._clock()
        task = asyncio.create_task(
            self._run_world(channel_id, world, self._stopping.get(channel_id))
        )
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

    async def _run_world(
        self, channel_id: str, world: TouhouWorld, previous: asyncio.Task | None = None
    ) -> None:
        """频道世界的主循环：包在租户作用域里跑（配额据此记账）。

        Args:
            channel_id: 频道标识
            world: 本频道的世界
            previous: 同名旧世界仍在关闭时的任务——先等它 flush 完再启动，
                否则新世界 restore 读到旧存档、旧世界随后的 flush 又覆盖新状态
        """
        with tenant_scope(channel_id):
            try:
                if previous is not None:
                    await asyncio.wait({previous})
                await world.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(f"频道 {channel_id} 的世界异常退出")
            finally:
                # 只摘自己：关闭期间同名频道可能已被新世界顶替，不能把新的摘掉
                channel = self._channels.get(channel_id)
                if channel is not None and channel.world is world:
                    del self._channels[channel_id]
                    if channel.mouth.subscriber_count:
                        self._orphaned[channel_id] = channel.mouth.subscribers()
                self._logger.info(f"频道世界已退出: {channel_id}")

    def __repr__(self) -> str:
        return f"ChannelHub(channels={self.channel_ids()})"


__all__ = ["Channel", "ChannelHub", "ChannelLimitError", "WorldFactory"]

"""WebSocket 服务入口 —— 多路频道 + 入口限流。

数据流：

    WS 连接 → 频道（channel_id）→ ChannelHub 队列 → 角色的世界
                    ↑                                    ↓
              入口令牌桶（超速当场回绝）        BroadcastMouth 广播回所有在线连接

要点：
- **多路复用**由 `ChannelHub` 承担：同频道的所有连接合流成一条快照流
- **入口限流**先于模型：超速只回一句提示，不消耗任何模型资源
- **输出流式**：角色说话经 `BroadcastMouth` 逐帧广播，多端同步看到「逐字蹦」
"""

import asyncio

from aiohttp import WSMsgType, web

from ...core.resource import IngressLimiter
from ...roleplay.hub import ChannelHub
from ...schemas.scene_schema import SceneType
from ...utils.logger import LoggerManager
from ...utils.text import strip_control_chars

_logger = LoggerManager.get_logger("WS")

HUB_KEY: web.AppKey[ChannelHub] = web.AppKey("hub", ChannelHub)
""" 应用上下文里的频道中枢键 """


class WsSink:
    """一个 WebSocket 连接作为广播订阅者（实现 `DeliverSink` 协议）。"""

    def __init__(self, ws: web.WebSocketResponse) -> None:
        """初始化。

        Args:
            ws: 已 prepare 的 WebSocket 响应对象
        """
        self._ws = ws

    async def deliver(self, frame: dict) -> None:
        """把一帧投给对端（连接已关闭时静默跳过）。

        Args:
            frame: 结构化帧
        """
        if not self._ws.closed:
            await self._ws.send_json(frame)


def build_app(
    *,
    hub: ChannelHub,
    limiter: IngressLimiter | None = None,
    default_channel: str = "lobby",
    heartbeat: float = 30.0,
) -> web.Application:
    """构建 aiohttp 应用（路由 `/ws/{channel}` 与 `/ws`）。

    Args:
        hub: 频道中枢
        limiter: 入口令牌桶；None 表示不限流
        default_channel: 未指定频道时使用的默认频道
        heartbeat: WS 心跳秒数

    Returns:
        web.Application: 可直接交给 `web.run_app` / `AppRunner`
    """

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        channel_id = request.match_info.get("channel") or request.query.get(
            "channel", default_channel
        )
        user = request.query.get("user", "访客")
        scene_type: SceneType = (
            "private_chat" if request.query.get("type") == "private" else "group_chat"
        )

        ws = web.WebSocketResponse(heartbeat=heartbeat)
        await ws.prepare(request)
        sink = WsSink(ws)
        hub.attach(channel_id, sink)
        _logger.info(
            f"WS 连接: channel={channel_id} user={user} 在线={hub.online_count(channel_id)}"
        )

        try:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    continue
                text = (msg.data or "").strip()
                if not text:
                    continue
                if limiter is not None:
                    wait = limiter.check(user)
                    if wait > 0:
                        await ws.send_json(
                            {
                                "type": "notice",
                                "text": f"说得好快呢……{wait:.1f} 秒后再试试吧～",
                            }
                        )
                        continue
                hub.submit(
                    channel_id,
                    user=user,
                    text=strip_control_chars(text),
                    scene_type=scene_type,
                    is_direct=scene_type == "private_chat",
                )
        finally:
            hub.detach(channel_id, sink)
            _logger.info(f"WS 断开: channel={channel_id} user={user}")
        return ws

    app = web.Application()
    app.router.add_get("/ws/{channel}", ws_handler)
    app.router.add_get("/ws", ws_handler)
    app[HUB_KEY] = hub
    return app


async def serve(
    hub: ChannelHub,
    *,
    host: str = "127.0.0.1",
    port: int = 8081,
    limiter: IngressLimiter | None = None,
) -> web.AppRunner:
    """启动 WS 服务（非阻塞），返回 runner 以便调用方控制生命周期。

    Args:
        hub: 频道中枢
        host: 监听地址
        port: 监听端口
        limiter: 入口令牌桶

    Returns:
        web.AppRunner: 已 setup + start 的 runner（调用方负责 cleanup）
    """
    app = build_app(hub=hub, limiter=limiter)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    _logger.info(f"WS 服务已启动: ws://{host}:{port}/ws/{{channel}}")
    return runner


async def _demo() -> None:  # pragma: no cover - 手动运行入口
    """最小可跑示例：本地起一个控制台配置的世界 + WS 服务。"""
    from ...core.bootstrap import discover_all
    from ...core.config import load_config
    from ...core.session_factory import build_resource_gate, build_session_manager
    from ...roleplay.character import load_character

    discover_all()
    config = load_config("config/settings.yaml")
    gate = build_resource_gate(config)
    sessions = build_session_manager(config, gate)
    character = load_character("config/roles/SaigyoujiYuyuko.yaml")
    hub = ChannelHub(sessions=sessions, character=character)
    runner = await serve(hub, limiter=IngressLimiter(rate=1.0, burst=3))
    try:
        await asyncio.Event().wait()
    finally:
        await hub.shutdown()
        await runner.cleanup()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_demo())

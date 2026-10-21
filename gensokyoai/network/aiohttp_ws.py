"""aiohttp 实现的 WebSocket 传输（network.base 的服务端与客户端）。"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping

import aiohttp
from aiohttp import WSMsgType, web

from .base import (
    Binary,
    Connected,
    Disconnected,
    Frame,
    Json,
    Message,
    ServerEvent,
    Text,
    Validate,
)


def classify(text: str) -> Frame:
    """文本帧分类：能解析成 dict/list 的给 Json，否则 Text。

    Json-over-text 协议（OneBot / 信封）在传输边缘分好类；裸数字/字符串
    这类"合法 JSON 但不是协议帧"的不算 Json。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return Text(text)
    return Json(data) if isinstance(data, dict | list) else Text(text)


class _Conn:
    """服务端侧的一条连接。"""

    def __init__(
        self, ws: web.WebSocketResponse, path: str, query: Mapping, remote: str | None
    ) -> None:
        self._ws = ws
        self.path = path
        self.query = query
        self.remote = remote

    async def send(self, frame: Frame | dict | list | str | bytes) -> None:
        if self._ws.closed:
            return
        if isinstance(frame, Text):
            await self._ws.send_str(frame.data)
        elif isinstance(frame, Binary):
            await self._ws.send_bytes(frame.data)
        elif isinstance(frame, Json):
            await self._ws.send_json(frame.data)
        elif isinstance(frame, dict | list):
            await self._ws.send_json(frame)
        elif isinstance(frame, bytes):
            await self._ws.send_bytes(frame)
        else:
            await self._ws.send_str(frame)

    async def close(self) -> None:
        if not self._ws.closed:
            await self._ws.close()


class _Pending:
    def __init__(self, request: web.Request, path: str) -> None:
        self.path = path
        self.query: Mapping[str, str] = request.query
        self.headers: Mapping[str, str] = request.headers
        self.remote = request.remote


class AiohttpWebsocketServer:
    """async-iterable 的 ws 服务端：连接生命周期与帧都进同一条事件队列。"""

    def __init__(
        self, host: str, port: int, *, validate: Validate | None = None
    ) -> None:
        """初始化。

        Args:
            host: 监听地址
            port: 监听端口
            validate: 握手策略（返回 (状态码, 原因) 拒绝；None 放行）
        """
        self._host = host
        self._port = port
        self._validate = validate
        self._queue: asyncio.Queue[ServerEvent] = asyncio.Queue()

    async def _entry(self, request: web.Request) -> web.StreamResponse:
        path = request.match_info.get("channel") or request.query.get(
            "channel", "lobby"
        )
        if self._validate is not None:
            reason = await self._validate(_Pending(request, path))
            if reason is not None:
                return web.Response(status=reason[0], text=reason[1])
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        conn = _Conn(ws, path, request.query, request.remote)
        await self._queue.put(Connected(conn))
        try:
            async for msg in ws:
                if msg.type is WSMsgType.TEXT and (text := (msg.data or "").strip()):
                    await self._queue.put(Message(conn, classify(text)))
                elif msg.type is WSMsgType.BINARY:
                    await self._queue.put(Message(conn, Binary(bytes(msg.data))))
        finally:
            await self._queue.put(Disconnected(conn))
        return ws

    def run(self) -> asyncio.Task:
        """起监听（返回 Task，取消即停服）。"""
        return asyncio.create_task(self._serve(), name=f"ws:{self._host}:{self._port}")

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_get("/ws/{channel}", self._entry)
        app.router.add_get("/ws", self._entry)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, self._host, self._port).start()
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    def __aiter__(self) -> AsyncIterator[ServerEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ServerEvent]:
        while True:
            yield await self._queue.get()


class AiohttpWebsocket:
    """ws 客户端：懒连接（首次收/发时才连）。"""

    def __init__(self, url: str) -> None:
        self._url = url
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def _ensure(self) -> None:
        if self._ws is not None and not self._ws.closed:
            return
        session = aiohttp.ClientSession()
        try:
            self._ws = await session.ws_connect(self._url, heartbeat=30)
        except BaseException:
            # 握手失败（403/503 等）时关掉刚建的会话
            await session.close()
            raise
        if self._session is not None and not self._session.closed:
            await self._session.close()  # 重连前收掉旧会话
        self._session = session

    def __aiter__(self) -> AsyncIterator[Frame]:
        return self._frames()

    async def _frames(self) -> AsyncIterator[Frame]:
        await self._ensure()
        async for msg in self._ws:
            if msg.type is WSMsgType.TEXT:
                yield classify(msg.data or "")
            elif msg.type is WSMsgType.BINARY:
                yield Binary(bytes(msg.data))

    async def send(self, frame: dict | list | str | bytes) -> None:
        await self._ensure()
        if isinstance(frame, dict | list):
            await self._ws.send_json(frame)
        elif isinstance(frame, bytes):
            await self._ws.send_bytes(frame)
        else:
            await self._ws.send_str(frame)

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

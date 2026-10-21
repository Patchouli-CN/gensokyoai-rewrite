"""网络层声明 —— 连接即事件源（async iteration）。

用法形状：

    server = WebsocketServer(url).run()       # -> asyncio.Task（生命周期归调用方）
    async for event in server: ...            # Connected / Message / Disconnected

    client = Websocket(url)                   # 客户端
    async for frame in client: ...            # Text / Binary / Json
    await client.send(frame)

没有回调注册：传输层是事件的生产者，应用层是消费者——和房间同构。
收发双方帧型一致：Text / Binary / Json（Json = 能解析成 dict/list 的文本帧，
JSON-over-text 协议在传输边缘就分好类）。
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Text:
    data: str


@dataclass(frozen=True, slots=True)
class Binary:
    data: bytes


@dataclass(frozen=True, slots=True)
class Json:
    """能解析成 dict/list 的文本帧（裸数字/字符串这类"合法 JSON 但
    不是协议帧"的不算）。"""

    data: dict | list


type Frame = Text | Binary | Json
""" 帧的三种形态（发帧：str 文本 / bytes 二进制 / dict|list JSON） """


class Conn(Protocol):
    """一条已建立的连接（事件里的主角）。"""

    path: str
    query: Mapping[str, str]
    remote: str | None

    async def send(self, frame: Frame | dict | list | str | bytes) -> None:
        """发帧（连接已关时实现方静默丢弃）。"""
        ...

    async def close(self) -> None:
        """主动关闭。"""
        ...


@dataclass(frozen=True, slots=True)
class Connected:
    conn: Conn


@dataclass(frozen=True, slots=True)
class Message:
    conn: Conn
    frame: Frame


@dataclass(frozen=True, slots=True)
class Disconnected:
    conn: Conn


type ServerEvent = Connected | Message | Disconnected
""" 服务端产出的事件流（客户端连接的生命周期 + 收到的帧） """


class Pending(Protocol):
    """握手待定：应用层只读元信息，返回拒绝理由或放行。"""

    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    remote: str | None


type Validate = Callable[[Pending], Awaitable[tuple[int, str] | None]]
""" 握手策略：返回 (HTTP 状态码, 原因) 拒绝；None 放行 """


class WebsocketServer(Protocol):
    """服务端：async-iterable 的事件源。"""

    def run(self) -> asyncio.Task:
        """起监听（返回 Task，取消即停服）。"""
        ...

    def __aiter__(self) -> AsyncIterator[ServerEvent]:
        """逐个产出连接事件（Connected/Message/Disconnected）。"""
        ...


class Websocket(Protocol):
    """客户端：发帧 + 收帧。"""

    def __aiter__(self) -> AsyncIterator[Frame]:
        """逐个产出收到的帧（Text/Binary/Json）。"""
        ...

    async def send(self, frame: dict | list | str | bytes) -> None:
        """发帧：dict/list 发 JSON、str 发文本、bytes 发二进制。"""
        ...

    async def close(self) -> None:
        """关闭连接。"""
        ...

"""口层（Mouth）—— 投递出口。console 打印；ws 广播 + ReplyStyle 出口。"""

from typing import Protocol, runtime_checkable

from .style import ReplyStyle
from .utils.text import strip_control_chars, strip_narration


@runtime_checkable
class Mouth(Protocol):
    """投递出口。"""

    async def send(self, name: str, text: str) -> None:
        """投递一条角色发言。"""
        ...


@runtime_checkable
class BroadcastMouth(Mouth, Protocol):
    """可挂连接的投递出口（网关依赖这三个方法）。"""

    def attach(self, sink) -> None:
        """挂一个连接。"""
        ...

    def detach(self, sink) -> None:
        """摘一个连接。"""
        ...

    def set_style(self, style: ReplyStyle) -> None:
        """声明回复风格。"""
        ...


class ConsoleMouth:
    """终端出口。"""

    async def send(self, name: str, text: str) -> None:
        """打印一行角色发言。"""
        print(f"{name}: {text}")


class WsMouth:
    """WebSocket 广播出口：一个房间的所有在线连接都收到发言。

    持有 ReplyStyle：禁叙述时出口剥括号块——剥过的文本才投递、
    由调用方入房（防描写进记忆当风格 few-shot）。
    """

    def __init__(self, style: ReplyStyle | None = None) -> None:
        self._style = style or ReplyStyle()
        self._style_locked = style is not None
        self._sinks: list = []

    def set_style(self, style: ReplyStyle) -> None:
        """声明风格（首连生效：已被声明过就不再更改）。"""
        if not self._style_locked:
            self._style = style
            self._style_locked = True

    def attach(self, sink) -> None:
        """挂一个连接（sink 需有 `async send(frame: dict)`）。"""
        self._sinks.append(sink)

    def detach(self, sink) -> None:
        """摘一个连接。"""
        if sink in self._sinks:
            self._sinks.remove(sink)

    @property
    def online(self) -> int:
        """在线连接数。"""
        return len(self._sinks)

    async def send(self, name: str, text: str) -> None:
        """广播一帧角色发言（按风格清洗后）。"""
        text = strip_control_chars(text)
        if not self._style.allow_narration:
            text = strip_narration(text)
        if not text:
            return
        frame = {"type": "message", "speaker": name, "text": text}
        for sink in list(self._sinks):
            try:
                await sink.send(frame)
            except Exception:  # noqa: BLE001 - 单个连接死了只摘它，不炸整屋广播
                self.detach(sink)

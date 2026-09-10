"""广播口 —— 频道作用域的输出投递。

一个频道可能挂着多个连接（多路），角色的每一句话要广播给**该频道所有在线连接**。
把 Mouth 做成「频道作用域」而不是给 `send` 加收件人参数，是更干净的切法：
- 单用户时退化为单播，不用改任何调用方
- 流式帧（begin/delta/end）逐帧广播，天然支持多端同步「逐字蹦」
"""

from typing import Protocol, runtime_checkable

from ..utils.logger import LoggerManager
from .base import Mouth


@runtime_checkable
class DeliverSink(Protocol):
    """可接收投递帧的订阅者（典型实现：一个 WebSocket 连接）。"""

    async def deliver(self, frame: dict) -> None:
        """投递一帧给对端。

        Args:
            frame: 结构化帧（如 {"type": "delta", "text": "..."}）
        """
        ...


class BroadcastMouth(Mouth):
    """广播口：把消息投给当前所有订阅者；无人订阅时静默丢弃。

    支持流式（`supports_streaming=True`）：begin/delta/end 逐帧广播。
    """

    supports_streaming = True

    def __init__(self) -> None:
        super().__init__()
        self._logger = LoggerManager.get_logger("MOUTH")
        self._sinks: set[DeliverSink] = set()

    def attach(self, sink: DeliverSink) -> None:
        """订阅（连接加入频道）。"""
        self._sinks.add(sink)

    def detach(self, sink: DeliverSink) -> None:
        """取消订阅（连接离开频道）。"""
        self._sinks.discard(sink)

    @property
    def subscriber_count(self) -> int:
        """当前订阅者数量。"""
        return len(self._sinks)

    async def _broadcast(self, frame: dict) -> None:
        """把一帧发给所有订阅者；投递失败的订阅者被摘除。"""
        for sink in list(self._sinks):
            try:
                await sink.deliver(frame)
            except Exception:
                self._logger.warning("投递失败，摘除该订阅者")
                self._sinks.discard(sink)

    async def send(self, speaker: str, text: str) -> None:
        """广播一条完整消息。"""
        await self._broadcast({"type": "message", "speaker": speaker, "text": text})

    async def begin(self, speaker: str) -> None:
        """广播一条流式消息的开头。"""
        self._stream_speaker = speaker
        self._stream_buf = []
        await self._broadcast({"type": "begin", "speaker": speaker})

    async def delta(self, text: str) -> None:
        """广播一块文本增量。"""
        self._stream_buf.append(text)
        if text:
            await self._broadcast({"type": "delta", "text": text})

    async def end(self) -> None:
        """广播一条流式消息的结束。"""
        await self._broadcast({"type": "end"})
        self._stream_buf = []

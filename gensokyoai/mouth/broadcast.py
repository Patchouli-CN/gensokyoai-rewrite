"""广播口 —— 频道作用域的输出投递。

一个频道可能挂着多个连接（多路），角色的每一句话要广播给**该频道所有在线连接**。
把 Mouth 做成「频道作用域」而不是给 `send` 加收件人参数，是更干净的切法：
- 单用户时退化为单播，不用改任何调用方
- 流式帧（begin/delta/end）逐帧广播，天然支持多端同步「逐字蹦」
"""

import asyncio
from typing import Protocol, runtime_checkable

from ..utils.logger import LoggerManager
from .base import Mouth

_DELIVER_TIMEOUT_S = 5.0
""" 单帧投递超时：超过即视为该连接卡死并摘除（不让一个慢客户端拖住整个频道）"""


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

    @property
    def has_audience(self) -> bool:
        """有订阅者才算有人在听。"""
        return bool(self._sinks)

    def subscribers(self) -> list[DeliverSink]:
        """当前订阅者快照（频道世界意外退出时，交接给同名新世界用）。"""
        return list(self._sinks)

    async def _broadcast(self, frame: dict) -> None:
        """把一帧并发发给所有订阅者；投递失败或超时的订阅者被摘除。

        此前逐个 await：一个不读数据的客户端（写缓冲满后 send 卡在 drain）会拖住
        其余所有连接的逐字流式，连带卡住世界的回合，直到心跳把它踢掉（30~45 秒）。
        """
        sinks = list(self._sinks)
        if not sinks:
            return
        results = await asyncio.gather(
            *(asyncio.wait_for(sink.deliver(frame), _DELIVER_TIMEOUT_S) for sink in sinks),
            return_exceptions=True,
        )
        for sink, result in zip(sinks, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                self._logger.warning(f"投递失败（{type(result).__name__}），摘除该订阅者")
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

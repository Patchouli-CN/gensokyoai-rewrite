"""控制台输出适配器 —— 默认实现：stdout 逐字打印。"""

from .base import Mouth


class ConsoleMouth(Mouth):
    """控制台场景：完整消息打印；`supports_streaming=True` 时逐块即时输出。"""

    supports_streaming = True

    async def send(self, speaker: str, text: str) -> None:
        """打印一条完整消息，格式与旧版 `print("\n{名字}: {文本}\n")` 一致。"""
        print(f"\n{speaker}: {text}\n")

    async def begin(self, speaker: str) -> None:
        """先输出 speaker 前缀，不换行，为逐块 `delta` 做准备。"""
        self._stream_speaker = speaker
        self._stream_buf = []
        print(f"\n{speaker}: ", end="", flush=True)

    async def delta(self, text: str) -> None:
        """逐块即时打印文本片段（终端可见流式效果）。"""
        self._stream_buf.append(text)
        if text:
            print(text, end="", flush=True)

    async def end(self) -> None:
        """收尾：补一行换行，结束本条流式消息。"""
        print("\n", flush=True)
        self._stream_buf = []

"""控制台输出适配器 —— 默认实现：stdout 逐字打印。"""

import sys

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

    async def send_voice(self, speaker: str, audio: bytes) -> None:
        """本机播放语音（Windows 用 winsound 异步放 wav；其他平台落盘提示）。"""
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(audio, winsound.SND_MEMORY | winsound.SND_ASYNC)
            return
        # 非 Windows 没有 stdlib 播放器：存到临时文件，让用户自己点
        import tempfile

        with tempfile.NamedTemporaryFile(
            prefix="gensokyoai_voice_", suffix=".wav", delete=False
        ) as tmp:
            tmp.write(audio)
            self._logger.info(f"{speaker} 的语音已存到: {tmp.name}")

"""口层抽象 —— 场景输出适配器（架构文档 §3.1 的对称输出层）。

与 eyes（输入感知）对称：eyes 看，mouth 说，brain 在中间想。
"""

from abc import ABC, abstractmethod

from ..utils.logger import LoggerManager


class Mouth(ABC):
    """口层：把「角色说了什么」投递到对应平台。

    Eyes 负责感知、Brain 负责决定、Responder 负责生成文本；
    Mouth 则把最终文本（含过渡语、开场白、主动发言）送到用户可见的平台。
    """

    supports_streaming: bool = False
    """ 该平台是否支持流式投递（边生成边显示）。默认不支持。 """

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger("MOUTH")
        self._stream_speaker: str = ""
        self._stream_buf: list[str] = []

    @abstractmethod
    async def send(self, speaker: str, text: str) -> None:
        """投递一条完整消息（缓冲路径：QQ 群聊等不支持流式的场景用这个）。

        Args:
            speaker: 说话者名字（如角色名）
            text: 完整消息文本
        """

    # ---- 流式接口：`supports_streaming=True` 的子类应覆盖 ----
    # 基类提供缓冲兜底实现：把内容攒起来，end 时一次性 send ——
    # 即使某个 mouth 没覆盖流式接口，loop 误走流式也不会崩。
    async def begin(self, speaker: str) -> None:
        """开始一条流式消息：先输出 speaker 前缀（仅流式场景调用）。"""
        self._stream_speaker = speaker
        self._stream_buf = []

    async def delta(self, text: str) -> None:
        """追加一块文本（仅流式场景调用）。基类默认缓冲。"""
        self._stream_buf.append(text)

    async def end(self) -> None:
        """结束一条流式消息：刷新/换行（仅流式场景调用）。基类默认一次性 send。"""
        await self.send(self._stream_speaker, "".join(self._stream_buf))

"""厂商无关的消息与结果 —— 适配器之间只认这一套。"""

import msgspec


class Text(msgspec.Struct, frozen=True):
    """一段文字。"""

    text: str


class Image(msgspec.Struct, frozen=True):
    """一张图（data URL 或 http 地址）。"""

    url: str
    label: str = ""


Part = Text | Image
""" 一条消息里的内容块 """


class ToolCall(msgspec.Struct, frozen=True):
    """模型发起的一次工具调用。"""

    name: str
    arguments: dict = msgspec.field(default_factory=dict)


class Turn(msgspec.Struct, frozen=True):
    """一轮发言（厂商无关）。"""

    role: str
    """ system / user / assistant / tool """

    parts: tuple[Part, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str = ""


class Usage(msgspec.Struct, frozen=True):
    """一次补全的用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入 + 输出。"""
        return self.prompt_tokens + self.completion_tokens


class Completion(msgspec.Struct, frozen=True):
    """适配器交回来的统一结果。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()
    model: str = ""
    finish_reason: str = ""

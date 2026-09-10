"""口层单元测试：抽象兜底流式 / ConsoleMouth 输出"""

from gensokyoai.mouth.base import Mouth
from gensokyoai.mouth.console import ConsoleMouth


class _RecordingMouth(Mouth):
    """只实现 send 的子类：记录投递内容，验证基类流式兜底"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str]] = []

    async def send(self, speaker: str, text: str) -> None:
        self.sent.append((speaker, text))


async def test_base_mouth_stream_fallback_buffers_then_sends():
    """基类 begin/delta/end 兜底：攒齐后一次性 send（未覆盖流式的 mouth 也不崩）"""
    m = _RecordingMouth()
    assert m.supports_streaming is False
    await m.begin("幽幽子")
    await m.delta("唔")
    await m.delta("……让我想想~")
    await m.end()
    assert m.sent == [("幽幽子", "唔……让我想想~")]


async def test_console_mouth_supports_streaming():
    """ConsoleMouth 默认支持流式投递"""
    assert ConsoleMouth().supports_streaming is True


async def test_console_mouth_send_prints_full(capsys):
    """send 完整打印：\\n{名字}: {文本}\\n\\n（沿用旧版 print 的三换行格式）"""
    await ConsoleMouth().send("幽幽子", "你好呀")
    captured = capsys.readouterr()
    assert captured.out == "\n幽幽子: 你好呀\n\n"


async def test_console_mouth_streams_deltas(capsys):
    """begin 打前缀、delta 逐块打印、end 补换行 —— 终端流式效果"""
    m = ConsoleMouth()
    await m.begin("幽幽子")
    await m.delta("唔")
    await m.delta("……")
    await m.end()
    captured = capsys.readouterr()
    assert captured.out == "\n幽幽子: 唔……\n\n"

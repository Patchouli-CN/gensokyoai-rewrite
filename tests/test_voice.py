"""嗓音层测试 —— 配置加载 / 渲染器旁路行为 / REPLY 事件发布。

协作者全假：GSVI 服务用假 client 顶（不依赖真实 TTS 服务），口层用记录型假嘴。
"""

import asyncio

from gensokyoai.core.event_bus import EventBus
from gensokyoai.schemas.event_schema import BaseEvent, EventTopic, ReplyPayload
from gensokyoai.voice import VoiceProfile, VoiceRenderer, VoiceSettings, load_voice_config
from gensokyoai.voice.renderer import attach_voice

_YUYUKO = VoiceProfile(
    gpt_weights="models/g/yuyuko.ckpt",
    sovits_weights="models/s/yuyuko.pth",
    ref_audio="D:/refs/yuyuko.wav",
    prompt_text="さて",
    prompt_lang="ja",
)

_SETTINGS = VoiceSettings(
    enabled=True,
    base_url="http://127.0.0.1:9880",
    text_lang="zh",
    characters={"西行寺幽幽子": _YUYUKO},
)


class _FakeClient:
    """假 GSVI 客户端：记录权重切换与合成调用，可注入故障"""

    def __init__(self, fail: bool = False) -> None:
        self.gpt: list[str] = []
        self.sovits: list[str] = []
        self.tts_calls: list[str] = []
        self.closed = False
        self._fail = fail

    async def set_gpt_weights(self, path: str) -> None:
        self.gpt.append(path)

    async def set_sovits_weights(self, path: str) -> None:
        self.sovits.append(path)

    async def tts(self, *, text, **kwargs) -> bytes:
        self.tts_calls.append(text)
        if self._fail:
            raise RuntimeError("服务炸了")
        return b"RIFF-fake-wav"

    async def aclose(self) -> None:
        self.closed = True


class _FakeMouth:
    """记录型假嘴"""

    def __init__(self) -> None:
        self.voices: list[tuple[str, bytes]] = []

    async def send_voice(self, speaker: str, audio: bytes) -> None:
        self.voices.append((speaker, audio))


def _event(character: str, text: str) -> BaseEvent:
    return EventBus.new(
        EventTopic.REPLY, source="test", payload=ReplyPayload(character=character, text=text)
    )


async def _drain(renderer: VoiceRenderer) -> None:
    if renderer._tasks:
        await asyncio.gather(*renderer._tasks, return_exceptions=True)


def test_load_voice_config(tmp_path):
    """yaml 正常解析：角色档案、默认值、缺失返回 None"""
    assert load_voice_config(base_dir=tmp_path) is None

    (tmp_path / "gsvits.yaml").write_text(
        """
enabled: true
characters:
  西行寺幽幽子:
    gpt_weights: "models/g/yuyuko.ckpt"
    sovits_weights: "models/s/yuyuko.pth"
    ref_audio: "D:/refs/yuyuko.wav"
    prompt_text: "さて"
""",
        encoding="utf-8",
    )
    cfg = load_voice_config(base_dir=tmp_path)
    assert cfg is not None and cfg.enabled is True
    assert cfg.base_url == "http://127.0.0.1:9880"  # 默认值
    assert cfg.text_lang == "zh"
    profile = cfg.characters["西行寺幽幽子"]
    assert profile.prompt_lang == "ja"  # 档案默认值
    assert profile.gpt_weights == "models/g/yuyuko.ckpt"


async def test_renderer_skips_unknown_character_and_empty_text():
    """没配声线的角色 / 空文本 / 非 REPLY 载荷：都不派渲染任务"""
    client, mouth = _FakeClient(), _FakeMouth()
    renderer = VoiceRenderer(client, _SETTINGS, mouth)

    await renderer.handle_reply(_event("博丽灵梦", "你好"))
    await renderer.handle_reply(_event("西行寺幽幽子", "   "))
    await renderer.handle_reply(EventBus.new(EventTopic.REPLY, source="test", payload="str"))
    assert not renderer._tasks
    assert mouth.voices == []


async def test_renderer_switches_weights_once_per_character():
    """同一角色连说两句只切一次声线；换角色再切"""
    client, mouth = _FakeClient(), _FakeMouth()
    renderer = VoiceRenderer(client, _SETTINGS, mouth)

    await renderer.handle_reply(_event("西行寺幽幽子", "第一句"))
    await renderer.handle_reply(_event("西行寺幽幽子", "第二句"))
    await _drain(renderer)

    assert client.gpt == ["models/g/yuyuko.ckpt"]  # 只切了一次
    assert len(client.tts_calls) == 2
    assert mouth.voices == [("西行寺幽幽子", b"RIFF-fake-wav")] * 2


async def test_renderer_failure_never_propagates():
    """TTS 服务炸了：异常吞掉，不溅回总线/主链路"""
    client, mouth = _FakeClient(fail=True), _FakeMouth()
    renderer = VoiceRenderer(client, _SETTINGS, mouth)

    await renderer.handle_reply(_event("西行寺幽幽子", "你好"))
    await _drain(renderer)  # 不应抛异常
    assert mouth.voices == []  # 失败就不补发语音


async def test_shutdown_drains_and_closes():
    """SHUTDOWN：等在飞渲染收尾，关客户端连接池"""
    client, mouth = _FakeClient(), _FakeMouth()
    renderer = VoiceRenderer(client, _SETTINGS, mouth)

    await renderer.handle_reply(_event("西行寺幽幽子", "你好"))
    await renderer.handle_shutdown(EventBus.new(EventTopic.SHUTDOWN, source="test"))
    assert client.closed is True
    assert len(mouth.voices) == 1  # 关闭前那句还是渲染完了


def test_attach_voice_subscribes_reply_and_shutdown():
    """attach_voice：REPLY 与 SHUTDOWN 都挂上总线"""
    from gensokyoai.schemas.event_schema import EventTopic as T

    bus = EventBus()
    world = type("W", (), {"bus": bus})()
    renderer = attach_voice(world, _SETTINGS, _FakeMouth())  # type: ignore[arg-type]
    assert renderer.handle_reply in bus._handlers[T.REPLY]
    assert renderer.handle_shutdown in bus._handlers[T.SHUTDOWN]

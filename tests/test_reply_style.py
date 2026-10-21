"""回复风格（ReplyStyle）测试 —— 预设解析 / 格式尾注 / 叙述剥离 / 世界与频道接线 / WS 声明。

场景归属由接入侧声明、机制由框架提供：本文件验证框架侧的两个手段
（responder 尾注 + 投递层剥离）与声明链路（hub attach / ws query）都work。
"""

import pytest

from gensokyoai.schemas.style_schema import ReplyStyle, resolve_reply_style
from gensokyoai.utils.text import NarrationStripper, strip_narration

# ---------- 预设与解析 ----------


def test_resolve_known_presets():
    """三个场景预设 + free 都可按名解析"""
    assert resolve_reply_style("free") == ReplyStyle()
    assert resolve_reply_style("pure_dialogue").allow_narration is False
    assert resolve_reply_style("pure_dialogue").send_greeting is False
    assert resolve_reply_style("tavern").pov == "any"
    assert resolve_reply_style("rpg").pov == "third"


def test_resolve_unknown_raises_with_options():
    """未知名 fail fast，错误信息列出可选值（插件作者排错用）"""
    with pytest.raises(KeyError, match="pure_dialogue"):
        resolve_reply_style("pure-dialogue")  # 拼错：连字符不是下划线


def test_format_block_free_is_empty():
    """无约束风格（free/tavern）：不注入格式尾注，与旧行为逐字节一致"""
    assert ReplyStyle().format_block() == ""
    assert resolve_reply_style("tavern").format_block() == ""


def test_format_block_pure_dialogue():
    """群聊预设：第一人称 + 禁叙述两条铁律"""
    block = resolve_reply_style("pure_dialogue").format_block()
    assert "【输出格式】" in block
    assert "第一人称" in block
    assert "不写动作、神态、心理或场景描写" in block


def test_format_block_rpg():
    """RPG 预设：第三人称旁白（叙述允许，不追加禁叙述行）"""
    block = resolve_reply_style("rpg").format_block()
    assert "第三人称" in block
    assert "不写动作" not in block


# ---------- 叙述剥离（缓冲 / 流式） ----------


def test_strip_narration_removes_paren_blocks():
    """缓冲路径：括号叙述块被剥掉，对话保留，空行清理"""
    text = "（抬起头，第三只眼微微转动）\n你好，灵梦。\n（轻轻合上书本）\n要坐坐吗？"
    assert strip_narration(text) == "你好，灵梦。\n要坐坐吗？"


def test_strip_narration_halfwidth_and_inline():
    """半角括号与行内括号块同样剥离"""
    assert strip_narration("你好(微笑)世界") == "你好世界"
    assert strip_narration("（叹气）唉。") == "唉。"


def test_narration_stripper_streaming_split_deltas():
    """流式路径：叙述块横跨 delta 也能剥；未闭合块的尾部吞掉（宁缺勿滥）"""
    stripper = NarrationStripper()
    assert stripper.feed("你好") == "你好"
    assert stripper.feed("（抬") == ""
    assert stripper.feed("头）") == ""
    assert stripper.feed("世界") == "世界"
    # 落单闭括号不在块内：原样放行
    assert stripper.feed("）") == "）"


# ---------- 投递层接线 ----------

from gensokyoai.core.brain.gate import PresenceTracker
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.config import OOCJudgeSettings, StyleSettings
from gensokyoai.core.health import HealthMonitor
from gensokyoai.core.responder.generator import Responder
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.components.delivery import DeliveryService
from gensokyoai.roleplay.components.effort import EffortGovernor
from gensokyoai.roleplay.components.ooc_guard import OOCGuard
from gensokyoai.roleplay.components.parrot import ParrotGuard
from gensokyoai.schemas.brain_schema import BrainConclusion
from gensokyoai.schemas.model_schema import CompletionResult, StreamEvent
from gensokyoai.schemas.scene_schema import SceneSnapshot

_PERSONA_BRIEF = "【觉】地灵殿的主人"


class _StubBackend:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def chat(self, messages, **kwargs):
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResult(content=content, finish_reason="stop")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _StreamBackend:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def chat_stream(self, messages, **kwargs):
        for chunk in self.chunks:
            yield StreamEvent(delta=chunk)


class _StubMouth:
    def __init__(self, streaming: bool) -> None:
        self.supports_streaming = streaming
        self.sent: list[str] = []
        self.deltas: list[str] = []

    async def begin(self, name: str) -> None:
        pass

    async def delta(self, text: str) -> None:
        self.deltas.append(text)

    async def end(self) -> None:
        pass

    async def send(self, name: str, text: str) -> None:
        self.sent.append(text)


class _StubMemory:
    async def recent(self, n: int = 10, *, search_term: str | None = None):
        return []


def _delivery(backend, mouth, style: ReplyStyle) -> DeliveryService:
    sm = SessionManager()
    sm.set_default_backend(backend)
    responder = Responder(sm, persona=_PERSONA_BRIEF)
    ooc = OOCGuard(
        OOCDetector(SessionManager()),
        responder,
        _StubMemory(),
        HealthMonitor(),
        Character(CharacterCard(name="觉", system_prompt="地灵殿的主人")),
        _PERSONA_BRIEF,
        EffortGovernor(),
        None,
        OOCJudgeSettings(),
        retry=True,
        audit=True,
        generation=lambda: 0,
    )
    return DeliveryService(
        responder,
        mouth,
        "觉",
        PresenceTracker(),
        None,
        ParrotGuard(StyleSettings(), responder),
        ooc,
        reply_style=style,
    )


def _snapshot() -> SceneSnapshot:
    return SceneSnapshot(scene_type="group_chat", sender="灵梦", content="你好", is_direct=True)


async def test_buffered_delivery_strips_narration_when_style_forbids():
    """禁叙述风格：缓冲路径剥括号块，投递与返回值都是剥后文本（记忆不污染）"""
    backend = _StubBackend(["（第三只眼微微转动）你好，灵梦。"])
    mouth = _StubMouth(streaming=False)
    delivery = _delivery(backend, mouth, resolve_reply_style("pure_dialogue"))

    reply = await delivery.deliver(_snapshot(), BrainConclusion(intent="寒暄"), [])

    assert reply == "你好，灵梦。"
    assert mouth.sent == ["你好，灵梦。"]


async def test_stream_delivery_strips_narration_when_style_forbids():
    """禁叙述风格：流式路径逐块过滤，叙述块横跨 delta 也不漏"""
    backend = _StreamBackend(["（第三只眼", "微微转动）你好，", "灵梦。"])
    mouth = _StubMouth(streaming=True)
    delivery = _delivery(backend, mouth, resolve_reply_style("pure_dialogue"))

    reply = await delivery.deliver(_snapshot(), BrainConclusion(intent="寒暄"), [])

    assert reply == "你好，灵梦。"
    assert "".join(mouth.deltas) == "你好，灵梦。"


async def test_free_style_keeps_narration():
    """free 风格：括号叙述原样放行（旧行为）"""
    backend = _StubBackend(["（微笑）你好。"])
    mouth = _StubMouth(streaming=False)
    delivery = _delivery(backend, mouth, ReplyStyle())

    reply = await delivery.deliver(_snapshot(), BrainConclusion(intent="寒暄"), [])

    assert reply == "（微笑）你好。"


# ---------- 世界装配：尾注 + 开场白门控 ----------

from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.satori.queue import QueuePerceiver


def _make_world(style: ReplyStyle, **kwargs) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend([]))
    character = Character(
        CharacterCard(name="觉", system_prompt="地灵殿的主人", greeting="我是古明地觉。")
    )
    return TouhouWorld(
        eye=QueuePerceiver(), character=character, sessions=sessions, reply_style=style, **kwargs
    )


def test_world_default_style_is_free():
    """不传风格：free（不设防，与旧行为一致）"""
    world = _make_world(ReplyStyle())
    assert world.reply_style == ReplyStyle()
    assert world._styled_persona() == "【觉】\n地灵殿的主人"


def test_world_pure_dialogue_appends_format_block():
    """pure_dialogue：responder 人设尾部追加【输出格式】块（卡面本身不动）"""
    world = _make_world(resolve_reply_style("pure_dialogue"))
    persona = world._styled_persona()
    assert persona.startswith("【觉】\n地灵殿的主人")
    assert "【输出格式】" in persona
    assert "第一人称" in persona


async def test_greeting_suppressed_when_style_forbids():
    """禁开场白风格：世界启动不发开场白"""
    mouth = _StubMouth(streaming=False)
    world = _make_world(resolve_reply_style("pure_dialogue"), mouth=mouth)
    await world._greet()
    assert mouth.sent == []


async def test_greeting_sent_when_style_allows():
    """默认风格：开场白照常发出"""
    mouth = _StubMouth(streaming=False)
    world = _make_world(ReplyStyle(), mouth=mouth)
    await world._greet()
    assert mouth.sent == ["我是古明地觉。"]


# ---------- 频道中枢：首连生效 ----------

from gensokyoai.roleplay.hub import ChannelHub


class _NullSink:
    async def deliver(self, frame: dict) -> None:
        pass


async def test_hub_attach_style_reaches_world(tmp_path):
    """attach 声明的风格进入世界构造（首连生效）"""
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend([]))
    hub = ChannelHub(
        sessions=sessions,
        character=Character(CharacterCard(name="觉", system_prompt="地灵殿的主人")),
        storage_dir=tmp_path,
    )
    style = resolve_reply_style("pure_dialogue")
    channel = hub.attach("g1", _NullSink(), style=style)
    assert channel.world.reply_style == style


async def test_hub_first_attach_wins_on_mismatch(tmp_path):
    """频道已存活时后来的不同声明被忽略（世界风格不变）"""
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend([]))
    hub = ChannelHub(
        sessions=sessions,
        character=Character(CharacterCard(name="觉", system_prompt="地灵殿的主人")),
        storage_dir=tmp_path,
    )
    hub.attach("g1", _NullSink(), style=resolve_reply_style("pure_dialogue"))
    channel = hub.attach("g1", _NullSink(), style=resolve_reply_style("rpg"))
    assert channel.world.reply_style.pov == "first", "首连的风格生效，后来的声明忽略"


async def test_hub_no_style_defaults_free(tmp_path):
    """不声明风格：free（旧调用方零改动）"""
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend([]))
    hub = ChannelHub(
        sessions=sessions,
        character=Character(CharacterCard(name="觉", system_prompt="地灵殿的主人")),
        storage_dir=tmp_path,
    )
    channel = hub.attach("g1", _NullSink())
    assert channel.world.reply_style == ReplyStyle()


# ---------- WS 声明链路（?style= 查询参数） ----------

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from gensokyoai.backends.ws_server import build_app
from gensokyoai.backends.ws_server import server as server_module


def _build_ws(tmp_path):
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend([]))
    hub = ChannelHub(
        sessions=sessions,
        character=Character(CharacterCard(name="觉", system_prompt="地灵殿的主人")),
        storage_dir=tmp_path,
        idle_ttl=0.0,
    )
    return hub, build_app(hub=hub)


async def test_ws_style_query_reaches_world(tmp_path):
    """本机接入侧声明 ?style=pure_dialogue：世界按声明构造"""
    hub, app = _build_ws(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&style=pure_dialogue")
        assert hub.worlds()["g1"].reply_style.pov == "first"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_ws_unknown_style_rejected(tmp_path):
    """未知风格名：握手 400 fail fast（拼错词不静默降级成 free）"""
    hub, app = _build_ws(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/ws/g1?user=甲&style=pure-dialogue")
        assert exc.value.status == 400
    finally:
        await client.close()
        await hub.shutdown()


async def test_ws_style_ignored_from_public_peer(tmp_path, monkeypatch):
    """公网客户端自报 style 一律作废（与 role 同一信任边界）"""
    monkeypatch.setattr(server_module, "_is_loopback", lambda remote: False)
    hub, app = _build_ws(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&style=rpg")
        assert hub.worlds()["g1"].reply_style == ReplyStyle(), "公网自报不认，落回 free"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()

"""ws 网关测试 —— 假 Pending/事件直接驱动（不碰 aiohttp）。"""

from gensokyoai.hub import Hub
from gensokyoai.loop import Loop
from gensokyoai.model import Reply
from gensokyoai.mouth import WsMouth
from gensokyoai.network.aiohttp_ws import classify
from gensokyoai.network.base import Binary, Connected, Disconnected, Json, Message, Text
from gensokyoai.room import Room
from gensokyoai.ws_server import Gateway, _parse_frame


class _Mind:
    name = "觉"

    async def step(self, feed: dict) -> Reply:
        return Reply()


class _FakePending:
    def __init__(self, path: str, query: dict, remote: str = "127.0.0.1") -> None:
        self.path = path
        self.query = query
        self.headers: dict = {}
        self.remote = remote


class _FakeConn:
    def __init__(self, path: str = "g1", query: dict | None = None) -> None:
        self.path = path
        self.query = query or {}
        self.remote = "127.0.0.1"
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        pass


def _hub() -> Hub:
    return Hub(lambda rid: Loop(Room(rid), _Mind(), WsMouth()))


def _gateway(hub: Hub | None = None, **kwargs) -> Gateway:
    return Gateway(hub or _hub(), **kwargs)


# ---------- 帧解析 ----------


def test_parse_plain_text():
    assert _parse_frame(Text("你好"), "访客", False, "") == ("访客", False, "你好", "")


def test_parse_envelope_overrides_user_and_direct():
    frame = Json(
        {"text": "觉，在吗", "user": "帕秋莉", "direct": True, "role": "admin"}
    )
    assert _parse_frame(frame, "访客", False, "") == (
        "帕秋莉",
        True,
        "觉，在吗",
        "admin",
    )


def test_parse_binary_dropped():
    """Binary 没有聊天语义：不入房"""
    assert _parse_frame(Binary(b"\x00\x01"), "访客", False, "") == (
        "访客",
        False,
        "",
        "",
    )


# ---------- 握手策略 ----------


async def test_reject_bad_channel():
    gateway = _gateway()
    assert (await gateway.validate(_FakePending("..%2f", {})))[0] == 400


async def test_reject_bad_token():
    gateway = _gateway(token="right")
    assert (await gateway.validate(_FakePending("g1", {"token": "wrong"})))[0] == 403


async def test_reject_unknown_style_from_loopback():
    gateway = _gateway()
    status, reason = await gateway.validate(
        _FakePending("g1", {"style": "pure-dialogue"})
    )
    assert status == 400 and "pure_dialogue" in reason


async def test_style_from_public_peer_ignored():
    """公网自报 style 不认（信任边界），握手放行"""
    gateway = _gateway()
    assert (
        await gateway.validate(_FakePending("g1", {"style": "rpg"}, remote="8.8.8.8"))
        is None
    )


# ---------- 事件消费 ----------


async def test_message_flows_into_room_with_identity():
    """消息入房，带发送者标识与是否点名"""
    hub = _hub()
    gateway = _gateway(hub)
    conn = _FakeConn("g1", {"user": "帕秋莉"})

    await gateway.handle(Connected(conn))
    await gateway.handle(Message(conn, Json({"text": "觉，在吗", "direct": True})))

    event = hub.get("g1").room.events[0]
    assert event.text == "觉，在吗"
    assert event.sender == "帕秋莉"
    assert event.mentioned is True
    assert event.user_id == "帕秋莉"


async def test_connected_applies_style():
    """风格声明在首连时应用到 mouth"""
    hub = _hub()
    gateway = _gateway(hub)
    await gateway.validate(_FakePending("g1", {"style": "pure_dialogue"}))
    conn = _FakeConn("g1")

    await gateway.handle(Connected(conn))

    assert hub.get("g1").mouth._style.pov == "first"
    assert conn.sent == []


async def test_disconnected_detaches_sink():
    hub = _hub()
    gateway = _gateway(hub)
    conn = _FakeConn("g1")

    await gateway.handle(Connected(conn))
    assert hub.get("g1").mouth.online == 1
    await gateway.handle(Disconnected(conn))
    assert hub.get("g1").mouth.online == 0


async def test_disconnect_of_unknown_room_creates_nothing():
    """一条断开事件不该凭空造出房间和常驻任务"""
    hub = _hub()
    gateway = _gateway(hub)

    await gateway.handle(Disconnected(_FakeConn("ghost")))

    assert hub.room_ids() == []


# ---------- 帧分类 ----------


def test_classify_json_object_and_array():
    assert classify('{"text": "hi"}') == Json({"text": "hi"})
    assert classify("[1, 2]") == Json([1, 2])


def test_classify_plain_and_scalar_json_stays_text():
    assert classify("你好") == Text("你好")
    assert classify("123") == Text("123")
    assert classify("{坏掉的") == Text("{坏掉的")


async def test_command_reply_as_system_frame_not_in_room():
    """斜杠指令：回系统帧、不进房；未知指令静默"""
    from gensokyoai.command.builtin import build_executor
    from gensokyoai.health import HealthCenter

    hub = _hub()
    gateway = _gateway(hub, executor=build_executor(HealthCenter()))
    conn = _FakeConn("g1")

    await gateway.handle(Connected(conn))
    await gateway.handle(Message(conn, Text("/doctor")))

    assert conn.sent and conn.sent[0]["speaker"] == "系统"
    assert hub.get("g1").room.events == []

    await gateway.handle(Message(conn, Text("/不存在")))
    assert len(conn.sent) == 1

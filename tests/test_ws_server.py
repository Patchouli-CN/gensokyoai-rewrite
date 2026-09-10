"""WebSocket 服务后端测试：路由 / 投递帧 / 端到端流式 / 入口限流"""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from gensokyoai.backends.ws_server import WsSink, build_app
from gensokyoai.core.resource import IngressLimiter
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.hub import ChannelHub
from gensokyoai.schemas.model_schema import CompletionResult


class _FakeWs:
    """记录 send_json 调用的假 WS 对象。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed = False

    async def send_json(self, frame: dict) -> None:
        self.frames.append(frame)


class _EchoBackend:
    """brain 返回合法接力 JSON（一轮收尾），responder 返回固定回复。"""

    def __init__(self) -> None:
        self.reply_n = 0

    async def chat(self, messages, **kw):
        system_text = next((m.content for m in messages if m.role == "system"), "")
        if "决策模块" in system_text:
            return CompletionResult(
                content=(
                    '{"thought": "闲聊", "intent": "回应", "emotion": "平淡", '
                    '"action_hint": "回应", "confidence": 0.8, "need_continue_think": false}'
                )
            )
        self.reply_n += 1
        return CompletionResult(content=f"回复{self.reply_n}")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _build(tmp_path, limiter=None) -> tuple[ChannelHub, object]:
    """构建 频道中枢 + WS 应用。"""
    sessions = SessionManager()
    sessions.set_default_backend(_EchoBackend())
    hub = ChannelHub(
        sessions=sessions,
        character=Character(
            CharacterCard(name="幽幽子", system_prompt="白玉楼的主人", greeting="哟~")
        ),
        storage_dir=tmp_path,
        idle_ttl=0.0,
        world_kwargs={
            "ooc_audit": False,
            "ooc_retry": False,
            "stall_probability": 0.0,
            "initiative_interval": 9999.0,
        },
    )
    return hub, build_app(hub=hub, limiter=limiter)


def test_build_app_registers_routes(tmp_path):
    """注册了带频道与默认频道两条路由"""
    _, app = _build(tmp_path)
    paths = {route.resource.canonical for route in app.router.routes()}
    assert "/ws/{channel}" in paths
    assert "/ws" in paths


async def test_ws_sink_forwards_and_skips_closed():
    """WsSink 转发帧；连接已关闭时静默跳过"""
    ws = _FakeWs()
    sink = WsSink(ws)

    await sink.deliver({"type": "message", "text": "hi"})
    assert ws.frames == [{"type": "message", "text": "hi"}]

    ws.closed = True
    await sink.deliver({"type": "message", "text": "again"})
    assert len(ws.frames) == 1


async def test_ws_end_to_end_streams_reply(tmp_path):
    """端到端：连接 → 发消息 → 收到开场白与流式回复"""
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    frames: list[dict] = []
    try:
        ws = await client.ws_connect("/ws/room?user=小明")
        await asyncio.sleep(0.05)  # 等频道世界启动
        await ws.send_str("你好")

        got_reply = False
        for _ in range(60):
            try:
                frame = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
            except TimeoutError:
                break
            frames.append(frame)
            if "回复" in frame.get("text", ""):
                got_reply = True
                break

        assert got_reply, f"应收到流式回复，实际帧={frames}"
        assert any(f.get("text") == "哟~" for f in frames), "全新会话应广播开场白"
        assert not ws.closed
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_ws_ingress_limiter_replies_notice(tmp_path):
    """入口限流：超速的消息被当场回绝（notice），不进入模型"""
    hub, app = _build(tmp_path, limiter=IngressLimiter(rate=0.001, burst=1))
    client = TestClient(TestServer(app))
    await client.start_server()
    frames: list[dict] = []
    try:
        ws = await client.ws_connect("/ws/room?user=小明")
        await asyncio.sleep(0.05)
        await ws.send_str("第一条")
        await ws.send_str("第二条")  # 令牌已用尽 -> 应收到 notice

        got_notice = False
        for _ in range(60):
            try:
                frame = await asyncio.wait_for(ws.receive_json(), timeout=3.0)
            except TimeoutError:
                break
            frames.append(frame)
            if frame.get("type") == "notice":
                got_notice = True
                break

        assert got_notice, f"应收到限流提示，实际帧={frames}"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()

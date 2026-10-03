"""WS 入口攻击测试：频道名路径逃逸、接入令牌、频道数上限。

攻击面：channel_id 会原样变成 session_id 并拼进落盘路径——
路径穿越、编码变体、同形字全都要在入口拦下。
"""

import pytest

from gensokyoai.backends.ws_server.server import _check_token
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.hub import ChannelHub, ChannelLimitError
from gensokyoai.utils.text import is_safe_channel_id


def test_channel_id_rejects_path_traversal():
    """路径穿越全家桶：点、斜杠、反斜杠、百分号编码、Unicode 同形字全拦"""
    for bad in [
        "../..",
        "..\\..\\windows",
        "a/b",
        "a\\b",
        ".",
        "..",
        "...",
        "..%2f..",
        "%2e%2e%2f",
        "a%00b",
        "a\x00b",
        "..／..／etc",  # 全角斜杠
        "‥/etc",  # Unicode 双点同形字
        "频道",
        "a b",
        "a.b",
        "",
        "a" * 65,  # 超长度上限
    ]:
        assert not is_safe_channel_id(bad), f"应拒绝: {bad!r}"


def test_channel_id_accepts_normal_names():
    """正常频道名放行"""
    for good in ["lobby", "marisa-room_01", "A9-_", "qun" + "1" * 59]:
        assert is_safe_channel_id(good), f"应放行: {good!r}"


def test_safe_channel_id_stays_inside_storage(tmp_path):
    """安全频道名拼进落盘路径后，解析结果必须仍在存储根目录内（纵深验证）"""
    root = tmp_path.resolve()
    for channel_id in ["lobby", "room_01", "A9-_"]:
        for key in [f"sessions/{channel_id}/session", f"traces/{channel_id}"]:
            resolved = (root / f"{key}.json").resolve()
            assert resolved.is_relative_to(root), f"路径逃逸: {key}"


def test_check_token():
    """接入令牌：未配置放行；配置了必须精确匹配（缺失/错误/空串都拒绝）"""
    assert _check_token(None, None)
    assert _check_token("anything", None)
    assert _check_token("s3cret", "s3cret")
    assert not _check_token(None, "s3cret")
    assert not _check_token("", "s3cret")
    assert not _check_token("s3cret ", "s3cret")  # 带空格不算
    assert not _check_token("S3CRET", "s3cret")  # 大小写敏感


def _char() -> Character:
    return Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人", greeting="哟~"))


async def test_hub_rejects_channel_flood():
    """频道数上限：刷随机频道名制造世界任务在第 max_channels+1 个被拒"""

    def factory(channel_id, perceiver, mouth):
        return _FakeWorld(perceiver)

    hub = ChannelHub(
        sessions=SessionManager(), character=_char(), world_factory=factory, max_channels=2
    )
    try:
        hub.attach("room-a", _NullSink())
        hub.attach("room-b", _NullSink())
        with pytest.raises(ChannelLimitError):
            hub.attach("room-c", _NullSink())
        # 已有频道不受上限影响
        hub.attach("room-a", _NullSink())
        assert hub.online_count("room-a") == 2
    finally:
        await hub.shutdown()


class _FakeWorld:
    """空转世界：只消费快照不调用任何模型。"""

    def __init__(self, perceiver) -> None:
        self._eye = perceiver

    async def start(self) -> None:
        while await self._eye.next_snapshot() is not None:
            pass


class _NullSink:
    async def deliver(self, frame: dict) -> None:
        pass


# ---------- 公网攻击面加固：role 信任边界 / 长度上限 / 限流键 / 拒裸奔 ----------

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from gensokyoai.backends.ws_server import build_app
from gensokyoai.backends.ws_server import server as server_module
from gensokyoai.backends.ws_server.commands import WsCommandState, build_executor
from gensokyoai.backends.ws_server.server import _MAX_TEXT_LEN, _is_loopback, main
from gensokyoai.core.config import GensokyoConfig, WorldSettings
from gensokyoai.core.resource import IngressLimiter
from gensokyoai.schemas.model_schema import CompletionResult


class _EchoBackend:
    async def chat(self, messages, **kw):
        return CompletionResult(content="回复")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _build(tmp_path, limiter=None):
    sessions = SessionManager()
    sessions.set_default_backend(_EchoBackend())
    hub = ChannelHub(
        sessions=sessions,
        character=_char(),
        storage_dir=tmp_path,
        idle_ttl=0.0,
        world_settings=WorldSettings(ooc_audit=False, stall_probability=0.0),
    )
    executor = build_executor(
        WsCommandState(sessions=sessions, hub=hub, config=GensokyoConfig(), version="0.0.19")
    )
    return hub, build_app(hub=hub, limiter=limiter, executor=executor)


async def _collect(ws, seconds=0.4) -> list[dict]:
    frames: list[dict] = []
    try:
        while True:
            frames.append(await asyncio.wait_for(ws.receive_json(), timeout=seconds))
    except TimeoutError:
        pass
    return frames


def test_is_loopback():
    """回环判定：role 信任边界的基石（IPv4/IPv6/主机名/非公网/垃圾输入）"""
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("127.0.0.2")
    assert _is_loopback("::1")
    assert _is_loopback("localhost")
    assert not _is_loopback("8.8.8.8")
    assert not _is_loopback("10.0.0.1"), "内网但非回环：经反代时不该信"
    assert not _is_loopback(None)
    assert not _is_loopback("垃圾")


async def test_role_ignored_from_public_peer(tmp_path, monkeypatch):
    """公网客户端自报 role 一律作废（/status 需要 USER，拿不到就是 VISITOR）"""
    monkeypatch.setattr(server_module, "_is_loopback", lambda remote: False)
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&role=owner")
        await ws.send_str("/status")
        frames = await _collect(ws)
        assert not any("已运行" in f.get("text", "") for f in frames), frames
        # 信封逐条覆盖同样不认
        await ws.send_str('{"text": "/status", "role": "member"}')
        frames = await _collect(ws)
        assert not any("已运行" in f.get("text", "") for f in frames), frames
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_role_honored_from_loopback(tmp_path, monkeypatch):
    """回环连接（本机插件）的 role 自报照常生效——信任链没修断"""
    monkeypatch.setattr(server_module, "_is_loopback", lambda remote: True)
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&role=member")
        await ws.send_str("/status")
        frames = await _collect(ws)
        assert any("已运行" in f.get("text", "") for f in frames), frames
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_oversize_message_rejected_at_gate(tmp_path):
    """超长消息入口截断（token 放大器）：>上限回 notice 不进会话；=上限放行"""
    hub, app = _build(tmp_path)
    submits: list[dict] = []
    original_submit = hub.submit

    def _spy(channel_id, **kwargs):
        submits.append(kwargs)
        original_submit(channel_id, **kwargs)

    hub.submit = _spy  # type: ignore[method-assign]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲")
        await ws.send_str("长" * (_MAX_TEXT_LEN + 1))
        frames = await _collect(ws)
        assert any("太长" in f.get("text", "") for f in frames), frames
        assert submits == [], "超长消息不应进会话"

        await ws.send_str("短" * _MAX_TEXT_LEN)
        frames = await _collect(ws, seconds=3.0)
        assert not any("太长" in f.get("text", "") for f in frames), frames
        assert len(submits) == 1, "上限内消息应正常进会话"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_limiter_keys_on_ip_not_nickname(tmp_path):
    """限流键是来源 IP：换昵称不开新桶（同 IP 的第二个「用户」照样被限）"""
    hub, app = _build(tmp_path, limiter=IngressLimiter(rate=0.001, burst=1))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws1 = await client.ws_connect("/ws/g1?user=小明")
        await ws1.send_str("第一条")  # 耗尽本 IP 唯一的令牌
        await asyncio.sleep(0.05)

        ws2 = await client.ws_connect("/ws/g1?user=大红")  # 换昵称不换 IP
        await ws2.send_str("换个名字继续刷")
        frames = await _collect(ws2, seconds=3.0)
        assert any(f.get("type") == "notice" for f in frames), frames
        await ws1.close()
        await ws2.close()
    finally:
        await client.close()
        await hub.shutdown()


def test_main_refuses_public_bind_without_token(capsys):
    """非回环监听 + 无 token：拒绝启动（不再是警告放行）"""
    rc = main(["--host", "0.0.0.0", "--port", "0"])
    assert rc == 2
    assert "--token" in capsys.readouterr().err

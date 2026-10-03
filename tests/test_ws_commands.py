"""ws_server 斜杠指令测试：/help /status /quota 分流、权限、静默规则、不进会话"""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from gensokyoai.backends.ws_server import build_app
from gensokyoai.backends.ws_server.commands import WsCommandState, build_executor, role_level
from gensokyoai.command import PermissionLevel
from gensokyoai.core.config import GensokyoConfig, WorldSettings
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.hub import ChannelHub
from gensokyoai.schemas.model_schema import CompletionResult


class _EchoBackend:
    async def chat(self, messages, **kw):
        return CompletionResult(content="回复")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _build(tmp_path):
    sessions = SessionManager()
    sessions.set_default_backend(_EchoBackend())
    hub = ChannelHub(
        sessions=sessions,
        character=Character(CharacterCard(name="纯狐", system_prompt="神灵")),
        storage_dir=tmp_path,
        idle_ttl=0.0,
        world_settings=WorldSettings(ooc_audit=False, stall_probability=0.0),
    )
    executor = build_executor(
        WsCommandState(sessions=sessions, hub=hub, config=GensokyoConfig(), version="0.0.19")
    )
    return hub, build_app(hub=hub, executor=executor)


async def _collect(ws, seconds=0.4) -> list[dict]:
    frames: list[dict] = []
    try:
        while True:
            frames.append(await asyncio.wait_for(ws.receive_json(), timeout=seconds))
    except TimeoutError:
        pass
    return frames


def test_role_level_mapping():
    assert role_level("owner") is PermissionLevel.OWNER
    assert role_level("admin") is PermissionLevel.ADMIN
    assert role_level("member") is PermissionLevel.USER
    assert role_level("guest") is PermissionLevel.VISITOR
    assert role_level(None) is PermissionLevel.VISITOR
    assert role_level("扯淡") is PermissionLevel.VISITOR


async def test_help_lists_commands_for_visitor(tmp_path):
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
        await ws.send_str("/help")
        frames = await _collect(ws)
        assert any("可用指令" in f.get("text", "") for f in frames), frames
        assert submits == [], "指令消息不应进会话（零模型调用）"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_help_aliases_chinese(tmp_path):
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲")
        await ws.send_str("/帮助")
        frames = await _collect(ws)
        assert any("可用指令" in f.get("text", "") for f in frames)
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_status_requires_user_role(tmp_path):
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # guest（VISITOR）调 /status（需 USER）→ 静默
        ws = await client.ws_connect("/ws/g1?user=甲&role=guest")
        await ws.send_str("/status")
        frames = await _collect(ws)
        assert not any("已运行" in f.get("text", "") for f in frames), "权限不足应静默"

        # 信封逐条覆盖 role：member 能看到状态
        await ws.send_str('{"text": "/status", "role": "member"}')
        frames = await _collect(ws)
        assert any("已运行" in f.get("text", "") for f in frames), frames
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_quota_shows_engine_costs(tmp_path):
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&role=member")
        await ws.send_str("/额度")
        frames = await _collect(ws)
        assert any("费用信息" in f.get("text", "") for f in frames), frames
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_unknown_command_silent(tmp_path):
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
        ws = await client.ws_connect("/ws/g1?user=甲&role=owner")
        await ws.send_str("/不存在的指令")
        frames = await _collect(ws)
        assert frames == [], "未知指令应对用户静默"
        assert submits == []
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_normal_chat_unaffected_by_executor(tmp_path):
    """带 executor 时普通消息照常进世界（不误伤聊天）"""
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/room?user=甲")
        await asyncio.sleep(0.05)
        await ws.send_str("你好")
        frames = await _collect(ws, seconds=3.0)
        assert any("回复" in f.get("text", "") for f in frames), frames
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_quota_reports_module_breakdown(tmp_path):
    """/quota：分模块消耗（含裁判这类无状态 owner）+ 合计 + 账户兜底行"""
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&role=owner")
        # 先聊一句让 responder/brain 产生真实记账
        await ws.send_str("你好")
        await _collect(ws, seconds=3.0)

        await ws.send_str("/quota")
        frames = await _collect(ws)
        text = next((f.get("text", "") for f in frames if "费用信息" in f.get("text", "")), "")
        assert "费用信息" in text
        assert "模块消耗" in text
        assert "表达(" in text or "大脑(" in text, text
        assert "合计:" in text and "缓存命中" in text
        assert "账户：" in text
        assert "暂无账户信息" in text, "本地假后端应落账户兜底行"
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()


async def test_quota_shows_rate_limit_windows(tmp_path, monkeypatch):
    """/quota：供应商自报的滚动窗口额度出现在账户区"""
    from gensokyoai.utils.ratelimit import RATE_LIMITS

    RATE_LIMITS.reset()
    hub, app = _build(tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws/g1?user=甲&role=member")
        RATE_LIMITS.note(
            "https://api.anthropic.com/v1",
            {
                "anthropic-ratelimit-requests-limit": "100",
                "anthropic-ratelimit-requests-remaining": "63",
                "anthropic-ratelimit-requests-reset": "2030-01-01T23:40:00Z",
            },
        )
        await ws.send_str("/quota")
        frames = await _collect(ws)
        text = next((f.get("text", "") for f in frames if "费用信息" in f.get("text", "")), "")
        assert "api.anthropic.com" in text
        assert "requests 剩余 63%" in text
        assert "重置" in text
        await ws.close()
    finally:
        await client.close()
        await hub.shutdown()
        RATE_LIMITS.reset()

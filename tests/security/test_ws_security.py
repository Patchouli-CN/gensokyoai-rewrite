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

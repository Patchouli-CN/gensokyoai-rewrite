"""WS 握手安全：频道、token 与不可信对端声明。"""

from gensokyoai.hub import Hub
from gensokyoai.ws_server import Gateway


class _Pending:
    def __init__(self, path: str, query: dict, remote: str = "127.0.0.1") -> None:
        self.path = path
        self.query = query
        self.headers: dict = {}
        self.remote = remote


def _gateway(**kwargs) -> Gateway:
    """纯握手测试不需要创建房间；若意外创建则立即失败。"""

    def fail(room_id: str):
        raise AssertionError(f"不应创建房间 {room_id}")

    return Gateway(Hub(fail), **kwargs)


async def test_reject_bad_channel():
    gateway = _gateway()
    assert (await gateway.validate(_Pending("..%2f", {})))[0] == 400


async def test_reject_bad_token():
    gateway = _gateway(token="right")
    assert (await gateway.validate(_Pending("g1", {"token": "wrong"})))[0] == 403


async def test_reject_unknown_style_from_loopback():
    gateway = _gateway()
    status, reason = await gateway.validate(_Pending("g1", {"style": "pure-dialogue"}))
    assert status == 400 and "pure_dialogue" in reason


async def test_style_from_public_peer_ignored():
    """公网自报 style 不认（信任边界），握手放行"""
    gateway = _gateway()
    assert (
        await gateway.validate(_Pending("g1", {"style": "rpg"}, remote="8.8.8.8"))
        is None
    )

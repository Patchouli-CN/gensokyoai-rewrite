"""fetch_url 重定向攻击测试：302 跳转是 SSRF 绕过的经典通道，每一跳都必须重新过门禁。

全程离线：用假 ClientSession 喂剧本，断言「请求发到了哪些 URL」。
"""

import pytest

from gensokyoai.tools.fetch_url import fetch_url


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data[:n]


class _FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b"") -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _FakeContent(body)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """按剧本回包，并记录每一个被请求的 URL。"""

    def __init__(self, script: list[_FakeResponse]) -> None:
        self._script = script
        self.requested: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self.requested.append(url)
        return self._script[len(self.requested) - 1]


def _install(monkeypatch: pytest.MonkeyPatch, script: list[_FakeResponse]) -> _FakeSession:
    session = _FakeSession(script)
    monkeypatch.setattr("aiohttp.ClientSession", lambda **kw: session)
    return session


async def test_redirect_to_loopback_blocked(monkeypatch):
    """公网 URL 302 到 127.0.0.1：第二跳在门禁被拦，绝不发请求"""
    session = _install(
        monkeypatch,
        [_FakeResponse(302, {"Location": "http://127.0.0.1:8080/v1/chat"})],
    )
    result = await fetch_url("http://example.com/article")
    assert "不允许访问" in result
    assert session.requested == ["http://example.com/article"], "第二跳不应发出请求"


async def test_redirect_to_decimal_ip_and_metadata_blocked(monkeypatch):
    """302 到十进制 IP（2130706433=127.0.0.1）与云元数据地址同样被拦"""
    for location in ["http://2130706433/", "http://169.254.169.254/latest/meta-data"]:
        session = _install(monkeypatch, [_FakeResponse(302, {"Location": location})])
        result = await fetch_url("http://example.com/")
        assert "不允许访问" in result, f"应拦截跳转到: {location}"
        assert session.requested == ["http://example.com/"]


async def test_redirect_to_public_followed(monkeypatch):
    """公网重定向正常跟随（含相对跳转），正文照常返回"""
    session = _install(
        monkeypatch,
        [
            _FakeResponse(302, {"Location": "/moved"}),
            _FakeResponse(
                200, {"Content-Type": "text/html"}, b"<html><body>hello world</body></html>"
            ),
        ],
    )
    result = await fetch_url("http://example.com/")
    assert "hello world" in result
    assert session.requested == ["http://example.com/", "http://example.com/moved"]


async def test_redirect_loop_capped(monkeypatch):
    """无限重定向被上限拦停"""
    script = [_FakeResponse(302, {"Location": f"http://example.com/hop{i}"}) for i in range(10)]
    session = _install(monkeypatch, script)
    result = await fetch_url("http://example.com/start")
    assert "重定向次数超过" in result
    assert len(session.requested) == 6, "首次 + 5 跳上限"

"""联网安全：私网拦截必须覆盖初始地址和每一个重定向跳点。"""

import pytest

from gensokyoai.tools.config import ToolSites
from gensokyoai.tools.web import WebClient, _assert_public


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class _Response:
    def __init__(
        self,
        url: str,
        status: int = 200,
        body: bytes = b"",
        headers: dict | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = headers or {}
        self.content = _Body(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _Client:
    def __init__(self, routes: dict[str, _Response]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, bool]] = []

    def get(self, url: str, allow_redirects: bool):
        self.calls.append((url, allow_redirects))
        return self.routes[url]


def _redirect(url: str, location: str) -> _Response:
    return _Response(url, status=302, headers={"Location": location})


def _install_fake_network(monkeypatch, client: WebClient, routes):
    """安装假响应，并记录每次公网校验；私网目标在校验处即失败。"""
    transport = _Client(routes)
    validated = []

    async def fake_assert_public(url: str) -> None:
        validated.append(url)
        if url.startswith(("http://127.0.0.1", "http://localhost", "http://10.")):
            raise ValueError("private target")

    monkeypatch.setattr(client, "_client", lambda: transport)
    monkeypatch.setattr("gensokyoai.tools.web._assert_public", fake_assert_public)
    return transport, validated


async def test_private_hosts_are_rejected():
    """内网字面地址一律不让抓（防 SSRF）"""
    for url in (
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://192.168.1.1/",
        "http://10.0.0.5/",
    ):
        with pytest.raises(ValueError):
            await _assert_public(url)


async def test_redirect_to_private_host_is_never_requested(monkeypatch):
    """公网入口跳到回环时，第二跳不能发出请求"""
    start = "https://public.test/start"
    client = WebClient()
    transport, validated = _install_fake_network(
        monkeypatch,
        client,
        {start: _redirect(start, "http://127.0.0.1/admin")},
    )

    with pytest.raises(ValueError, match="private target"):
        await client.fetch(start)

    assert transport.calls == [(start, False)]
    assert validated == [start, "http://127.0.0.1/admin"]


async def test_redirect_rechecks_site_rules(monkeypatch):
    """初始域名获准不代表重定向目标也获准"""
    start = "https://public.test/start"
    client = WebClient()
    client.fetch_sites = ToolSites(allow=("public.test",))
    transport, validated = _install_fake_network(
        monkeypatch,
        client,
        {start: _redirect(start, "https://blocked.test/landing")},
    )

    with pytest.raises(ValueError, match="站点规则"):
        await client.fetch(start)

    assert transport.calls == [(start, False)]
    assert validated == [start]


async def test_safe_redirect_is_followed_without_auto_redirect(monkeypatch):
    """安全的相对跳转仍正常工作，且全程关闭自动重定向"""
    start = "https://public.test/start"
    final = "https://public.test/final"
    client = WebClient()
    transport, _validated = _install_fake_network(
        monkeypatch,
        client,
        {
            start: _redirect(start, "/final"),
            final: _Response(final, body=b"<p>safe body</p>"),
        },
    )

    assert await client.fetch(start) == "safe body"
    assert transport.calls == [(start, False), (final, False)]


async def test_redirect_count_is_bounded(monkeypatch):
    """无限跳转不能变成无限请求"""
    start = "https://public.test/0"
    routes = {
        f"https://public.test/{index}": _redirect(
            f"https://public.test/{index}", f"/{index + 1}"
        )
        for index in range(7)
    }
    client = WebClient()
    transport, _validated = _install_fake_network(monkeypatch, client, routes)

    with pytest.raises(ValueError, match="重定向超过"):
        await client.fetch(start)

    assert len(transport.calls) == 6


async def test_sogou_resolve_drops_private_redirect(monkeypatch):
    """搜狗跳转解析到私网时，结果地址直接作废"""
    start = "https://www.sogou.com/link?url=abc"
    hit = {"url": start}
    client = WebClient()
    transport, _validated = _install_fake_network(
        monkeypatch,
        client,
        {start: _redirect(start, "http://10.0.0.1/internal")},
    )

    await client._resolve(hit)

    assert hit["url"] == ""
    assert transport.calls == [(start, False)]

"""fetch_url 工具测试：HTML 剥离 / 截断 / 异常降级（SSRF 门禁见 tests/security/）。

全程打桩 aiohttp，不联网（CI 无网络也能跑）。
"""

import pytest

from gensokyoai.core.brain.engine import build_tool_directive
from gensokyoai.core.config import KnowledgeSite
from gensokyoai.tools.fetch_url import fetch_url

# ---------- fetch_url ----------


class _FakeContent:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data if n < 0 else self._data[:n]


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"", content_type: str = "text/html"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """按类属性返回固定响应 / 抛固定异常的假 aiohttp 会话"""

    response: _FakeResponse | None = None
    error: Exception | None = None

    def __init__(self, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url: str, **kwargs):
        if _FakeSession.error is not None:
            raise _FakeSession.error
        return _FakeSession.response


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    _FakeSession.error = None
    yield _FakeSession
    _FakeSession.error = None
    _FakeSession.response = None


async def test_fetch_url_strips_html(fake_session):
    """正常抓取：剥 script/style/标签，实体反转义，空白收敛"""
    _FakeSession.response = _FakeResponse(
        body="<html><head><style>body{}</style></head>"
        "<body><script>var x=1;</script><p>你好&nbsp; <b>世界</b></p></body></html>".encode()
    )
    output = await fetch_url("https://thbwiki.cc/test")
    assert output == "你好 世界"


async def test_fetch_url_truncates_long_body(fake_session):
    """超长正文截断到 4000 字并标注"""
    _FakeSession.response = _FakeResponse(body=("很长" * 3000).encode())
    output = await fetch_url("https://example.com/long")
    assert "已截断" in output
    assert len(output) < 4100


async def test_fetch_url_http_error(fake_session):
    """HTTP 错误状态：返回状态码，不带正文"""
    _FakeSession.response = _FakeResponse(status=404, body=b"not found")
    output = await fetch_url("https://example.com/missing")
    assert "HTTP 404" in output


async def test_fetch_url_network_error(fake_session):
    """网络异常：人话提示，不炸调用方"""
    _FakeSession.error = TimeoutError("超时")
    output = await fetch_url("https://example.com/")
    assert "抓取失败" in output


async def test_fetch_url_blocks_ssrf():
    """内网地址在发起请求前就被门禁拦下"""
    output = await fetch_url("http://192.168.1.1/admin")
    assert "不允许访问" in output


# ---------- 可信知识站指令 ----------


def test_build_tool_directive_empty():
    """无站点配置：空串（不污染提示词）"""
    assert build_tool_directive([]) == ""


def test_build_tool_directive_lists_sites():
    """站点表拼成指令块，含优先策略与站点说明"""
    directive = build_tool_directive(
        [KnowledgeSite(site="thbwiki.cc", desc="东方中文维基"), KnowledgeSite(site="example.com")]
    )
    assert "可信知识站" in directive
    assert "thbwiki.cc：东方中文维基" in directive
    assert "- example.com" in directive
    assert "web_search" in directive

"""web_search 工具测试：格式化 / 空结果 / 异常降级 / 条数截断 / 缺依赖提示。

全程打桩 ddgs，不联网（CI 无网络也能跑）。
"""

import sys

import pytest

pytest.importorskip("ddgs", reason="ddgs 未安装（pip install 'gensokyoai[search]'）")

import ddgs.exceptions

from gensokyoai.tools.web_search import web_search

_FAKE_RESULTS = [
    {"title": "红魔馆", "body": "位于幻想乡的吸血鬼洋馆", "href": "https://example.com/1"},
    {"title": "无链接条目", "body": "没有 href 应被过滤", "href": ""},
    {"title": "白玉楼", "body": "冥界的亡灵居所", "href": "https://example.com/2"},
]


class _FakeDDGS:
    """记录入参、返回固定结果的假 DDGS"""

    def __init__(self, timeout=None):
        self.timeout = timeout

    def text(self, query, *, region, safesearch, max_results):
        _FakeDDGS.last_call = {
            "query": query,
            "region": region,
            "safesearch": safesearch,
            "max_results": max_results,
        }
        return _FAKE_RESULTS


@pytest.fixture
def fake_ddgs(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    return _FakeDDGS


def test_web_search_formats_results(fake_ddgs):
    """正常：编号列表（标题/摘要/链接），无 href 的条目被过滤"""
    output = web_search("幻想乡")
    assert "1. 红魔馆" in output
    assert "2. 白玉楼" in output
    assert "https://example.com/2" in output
    assert "无链接条目" not in output
    assert _FakeDDGS.last_call["max_results"] == 5


def test_web_search_clamps_max_results(fake_ddgs):
    """max_results 越界时收敛到 1~10"""
    web_search("测试", max_results=999)
    assert _FakeDDGS.last_call["max_results"] == 10
    web_search("测试", max_results=0)
    assert _FakeDDGS.last_call["max_results"] == 1


def test_web_search_empty_results(fake_ddgs, monkeypatch):
    """空结果：友好提示而非空串"""
    monkeypatch.setattr(_FakeDDGS, "text", lambda self, q, **kw: [])
    assert "没有找到" in web_search("不存在的东西")


def test_web_search_ddgs_exception(fake_ddgs, monkeypatch):
    """ddgs 异常：返回原因，不炸调用方"""

    def _boom(self, query, **kw):
        raise ddgs.exceptions.DDGSException("限流了")

    monkeypatch.setattr(_FakeDDGS, "text", _boom)
    assert web_search("测试").startswith("搜索失败")


def test_web_search_network_error(fake_ddgs, monkeypatch):
    """非 ddgs 的网络异常同样兜住"""

    def _boom(self, query, **kw):
        raise ConnectionResetError("连接被重置")

    monkeypatch.setattr(_FakeDDGS, "text", _boom)
    assert "网络异常" in web_search("测试")


def test_web_search_missing_dependency(monkeypatch):
    """未安装 ddgs：工具仍注册，调用返回安装提示"""
    monkeypatch.setitem(sys.modules, "ddgs", None)
    assert "未安装 ddgs" in web_search("测试")


def test_web_search_registered_after_discovery():
    """bootstrap 扫描后 web_search 已进工具表"""
    from gensokyoai.core.bootstrap import discover_all
    from gensokyoai.core.registry import ToolRegistry

    discover_all()
    assert ToolRegistry.get("web_search").tool_func is web_search

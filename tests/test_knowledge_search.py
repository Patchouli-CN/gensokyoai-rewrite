"""knowledge_search 工具测试 —— URL 构造收进代码 / 双响应方言 / 失败隔离 / 缓存接线。"""

import msgspec
import pytest

from gensokyoai.core.config import KnowledgeSite, SearchSettings
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.satori.queue import QueuePerceiver
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.tools import knowledge_search as kwiki
from gensokyoai.tools.knowledge_search import build_knowledge_search

_SITES = [KnowledgeSite(site="thbwiki.cc", desc="东方中文维基"), KnowledgeSite(site="example.com")]


def _api_url(site: str, query: str, limit: int = 5) -> str:
    from urllib.parse import quote

    return (
        f"https://{site}/api.php?action=opensearch&search={quote(query)}&limit={limit}&format=json"
    )


@pytest.fixture
def fake_get(monkeypatch):
    """拦截 guarded_get_text：记录请求 URL，按脚本返回/抛错"""
    calls: list[str] = []
    script: dict[str, object] = {}

    async def fake(url: str, *, timeout_s: float = 20.0) -> str:
        calls.append(url)
        result = script.get(url, Exception(f"未编排的 URL: {url}"))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(kwiki, "guarded_get_text", fake)
    return calls, script


async def test_hits_thbwiki_dialect(fake_get):
    """thbwiki 对象数组方言：编号列表（站点/标题/API 自报链接），URL 零拼装"""
    calls, script = fake_get
    script[_api_url("thbwiki.cc", "古明地恋")] = msgspec.json.encode(
        [{"text": "古明地恋", "link": "https://thbwiki.cc/古明地恋"}]
    ).decode()
    script[_api_url("example.com", "古明地恋")] = "[]"
    spec = build_knowledge_search(_SITES)

    result = await spec.tool_func("古明地恋")

    assert len(calls) == 2, "每个配置站查一次"
    assert "1. [thbwiki.cc] 古明地恋\n   https://thbwiki.cc/古明地恋" in result
    assert "传给 fetch_url" in result, "结果级使用提示（接力调用防呆）"


async def test_hits_standard_dialect(fake_get):
    """标准位置数组方言：[搜索词, [标题], [描述], [链接]] 同样归一"""
    _, script = fake_get
    script[_api_url("thbwiki.cc", "test")] = "[]"
    script[_api_url("example.com", "test")] = msgspec.json.encode(
        ["test", ["词条A"], [""], ["https://example.com/wiki/词条A"]]
    ).decode()
    spec = build_knowledge_search(_SITES)

    result = await spec.tool_func("test")

    assert "[example.com] 词条A" in result
    assert "https://example.com/wiki/词条A" in result


async def test_site_failure_isolated(fake_get):
    """单站失败不拖垮整次搜索：其余站照常返回，失败名单附注"""
    _, script = fake_get
    script[_api_url("example.com", "test")] = '[{"text": "T", "link": "https://example.com/T"}]'
    spec = build_knowledge_search(_SITES)

    result = await spec.tool_func("test")

    assert "[example.com] T" in result
    assert "thbwiki.cc 本次访问失败" in result


async def test_all_failed_or_empty_says_not_found(fake_get):
    """全部失败/空结果：「没有找到」前缀（知识缓存不缓存失败）"""
    spec = build_knowledge_search(_SITES)
    result = await spec.tool_func("啥都没有")
    assert result.startswith("没有找到")
    assert "web_search" in result, "应指引泛搜兜底"


async def test_blank_query_rejected():
    """空关键词：快速失败，不发请求"""
    spec = build_knowledge_search(_SITES)
    assert (await spec.tool_func("   ")).startswith("搜索失败")


# ---------- 世界装配与缓存接线 ----------


class _StubBackend:
    async def chat(self, *args, **kwargs):  # pragma: no cover - 不会被调到
        return CompletionResult(content="")


def _world(**kwargs) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend())
    character = Character(CharacterCard(name="觉", system_prompt="地灵殿的主人"))
    return TouhouWorld(eye=QueuePerceiver(), character=character, sessions=sessions, **kwargs)


def test_world_registers_knowledge_search_when_sites_configured():
    """配了可信站：工具进列表；没配：不注册"""
    with_sites = _world(search=SearchSettings(knowledge_sites=list(_SITES)))
    names = [t.tool_name for t in with_sites.brain._tools]
    assert "knowledge_search" in names

    without = _world()
    names = [t.tool_name for t in without.brain._tools]
    assert "knowledge_search" not in names


async def test_knowledge_search_wrapped_with_cache():
    """套缓存后同关键词第二次零网络（L1 命中）"""
    from gensokyoai.core.memorizer.knowledge import KnowledgeCache
    from gensokyoai.core.memorizer.manager import MemoryManager

    calls = 0

    async def fake_search(query: str, max_results: int = 5) -> str:
        nonlocal calls
        calls += 1
        return "1. 词条"

    cache = KnowledgeCache(MemoryManager(storage_dir=None, session_id=None))
    from gensokyoai.schemas.model_schema import ToolSpec

    [wrapped] = cache.wrap_all([ToolSpec(tool_func=fake_search, name="knowledge_search")])
    assert await wrapped.tool_func("古明地恋") == "1. 词条"
    assert "1. 词条" in await wrapped.tool_func("古明地恋")
    assert calls == 1, "第二次应命中 L1 缓存"

"""知识缓存测试：L1 TTL 命中 / L2 归档 / 时效词跳过 / 失败不缓存 / 包装透明性"""

from gensokyoai.core.memorizer.knowledge import KnowledgeCache
from gensokyoai.schemas.memory_schema import MemoryType
from gensokyoai.schemas.model_schema import ToolSpec


class _FakeMemory:
    """记录归档条目的假记忆管理器"""

    def __init__(self) -> None:
        self.items = []

    async def store(self, item) -> None:
        self.items.append(item)


def _counting_sync_tool(results: list[str]):
    """数调用次数的假同步搜索工具"""

    def web_search(query: str, max_results: int = 5) -> str:
        """假搜索"""
        web_search.calls += 1
        return results.pop(0) if len(results) > 1 else results[0]

    web_search.calls = 0
    return web_search


def _counting_async_tool(result: str):
    """数调用次数的假异步抓取工具"""

    async def fetch_url(url: str) -> str:
        """假抓取"""
        fetch_url.calls += 1
        return result

    fetch_url.calls = 0
    return fetch_url


def _wrap(cache: KnowledgeCache, func) -> ToolSpec:
    """把假工具包装成 ToolSpec 后过缓存层（模拟 loop._setup_tools 路径）"""
    [wrapped] = cache.wrap_all([ToolSpec(tool_func=func)])
    return wrapped


async def test_l1_hit_skips_original():
    """L1：TTL 内同查询第二次调用不碰原工具，且带命中标记"""
    tool = _counting_sync_tool(["幽幽子的设定"])
    wrapped = _wrap(KnowledgeCache(_FakeMemory()), tool)

    first = await wrapped.tool_func("幽幽子")
    second = await wrapped.tool_func("幽幽子")
    assert tool.calls == 1
    assert first == "幽幽子的设定"
    assert "缓存命中" in second


async def test_l1_expired_refetches():
    """L1：TTL=0 等于禁用，每次都真调"""
    tool = _counting_sync_tool(["结果"])
    wrapped = _wrap(KnowledgeCache(_FakeMemory(), ttl_s=0.0), tool)
    await wrapped.tool_func("幽幽子")
    await wrapped.tool_func("幽幽子")
    assert tool.calls == 2


async def test_l2_archives_as_knowledge():
    """L2：成功结果归档为 KNOWLEDGE 条目，importance 过归档线"""
    memory = _FakeMemory()
    tool = _counting_sync_tool(["幽幽子是亡灵公主"])
    wrapped = _wrap(KnowledgeCache(memory), tool)
    await wrapped.tool_func("幽幽子的设定")

    assert len(memory.items) == 1
    item = memory.items[0]
    assert item.memory_type == MemoryType.KNOWLEDGE
    assert item.importance >= 0.6
    assert "幽幽子" in item.topic


async def test_freshness_query_skips_l2():
    """时效词查询：L1 照缓存，但不进长期记忆（新闻会过期）"""
    memory = _FakeMemory()
    tool = _counting_sync_tool(["今天的新闻内容"])
    wrapped = _wrap(KnowledgeCache(memory), tool)
    await wrapped.tool_func("东方最新新闻")
    await wrapped.tool_func("东方最新新闻")
    assert tool.calls == 1  # L1 生效
    assert memory.items == []  # L2 跳过


async def test_failure_not_cached():
    """失败结果（搜索失败/没有找到）不缓存不归档，下次重试"""
    memory = _FakeMemory()
    tool = _counting_sync_tool(["搜索失败：限流了", "真实结果"])
    wrapped = _wrap(KnowledgeCache(memory), tool)
    assert (await wrapped.tool_func("幽幽子")).startswith("搜索失败")
    assert await wrapped.tool_func("幽幽子") == "真实结果"  # 第二次真调并拿到好结果
    assert tool.calls == 2
    assert len(memory.items) == 1


async def test_wrap_async_tool_and_name_transparent():
    """包装对工具链路透明：异步工具照常工作，工具名/参数签名不变"""
    tool = _counting_async_tool("抓到的正文")
    wrapped = _wrap(KnowledgeCache(_FakeMemory()), tool)

    assert wrapped.tool_name == "fetch_url"
    assert "url" in wrapped.params
    assert wrapped.is_async is True
    assert await wrapped.tool_func("https://thbwiki.cc/x") == "抓到的正文"


async def test_unrelated_tools_untouched():
    """非联网工具原样通过（不套缓存）"""
    cache = KnowledgeCache(_FakeMemory())

    def get_current_time() -> str:
        return "中午"

    [spec] = cache.wrap_all([ToolSpec(tool_func=get_current_time)])
    assert spec.tool_func is get_current_time

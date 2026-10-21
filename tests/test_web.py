"""联网工具测试 —— 解析、站点规则、内网拦截、继承发现。"""

import pytest

from gensokyoai.api import tools_of
from gensokyoai.tools import ToolSites, html_to_text, parse_bing
from gensokyoai.tools.config import load_sites
from gensokyoai.tools.web import WebTools, _assert_public

BING_HTML = """
<ol id="b_results">
<li class="b_algo"><h2><a href="https://thwiki.cc/觉">东方维基 - 觉</a></h2>
  <p>古明地觉是东方Project的角色。</p></li>
<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=1&amp;u=a1aHR0cHM6Ly9naXRodWIuY29tL2Zvby9iYXI&amp;ntb=1">GitHub</a></h2>
  <p>一个仓库。</p></li>
<li class="b_algo"><h2><a href="https://spam.example/xx">垃圾站</a></h2>
  <p>广告。</p></li>
</ol>
"""


def test_parse_bing_extracts_and_unwraps():
    """Bing 的跳转链接要还原成真实地址"""
    hits = parse_bing(BING_HTML)

    assert [hit["title"] for hit in hits] == ["东方维基 - 觉", "GitHub", "垃圾站"]
    assert hits[0]["url"] == "https://thwiki.cc/觉"
    assert hits[1]["url"] == "https://github.com/foo/bar", "ck/a 里的 base64 要解出来"
    assert hits[0]["snippet"] == "古明地觉是东方Project的角色。"


def test_parse_bing_ignores_non_http():
    """非 http 的结果跳过"""
    markup = '<li class="b_algo"><h2><a href="javascript:void(0)">x</a></h2></li></ol>'
    assert parse_bing(markup) == []


def test_sites_allow_and_block():
    """allow 是白名单，block 优先于 allow，域名按后缀匹配"""
    sites = ToolSites(allow=("thwiki.cc",), block=("m.thwiki.cc",))

    assert sites.allows("https://www.thwiki.cc/page")
    assert sites.allows("https://thwiki.cc/page")
    assert not sites.allows("https://github.com/x")
    assert not sites.allows("https://m.thwiki.cc/no"), "block 优先"


def test_sites_without_allow_allows_everything():
    """没配 allow 就全放行"""
    assert ToolSites().allows("https://anything.example/x")


def test_prefer_marks_domains():
    """prefer 只影响排序，不影响放行"""
    sites = ToolSites(prefer=("github.com",))

    assert sites.prefers("https://github.com/a")
    assert not sites.prefers("https://example.com/a")
    assert sites.allows("https://example.com/a")


def test_load_sites_reads_config_dir(tmp_path):
    """配置按 config/tools/<工具>/sites.yaml 找"""
    folder = tmp_path / "web_search"
    folder.mkdir()
    (folder / "sites.yaml").write_text(
        "allow: [thwiki.cc]\nprefer: [thwiki.cc]\n", encoding="utf-8"
    )

    sites = load_sites("web_search", tmp_path)

    assert sites.allow == ("thwiki.cc",)
    assert load_sites("fetch_url", tmp_path).allow == (), "没配置就全放行"


def test_html_to_text_strips_scripts_and_blank_lines():
    """网页转正文：脚本样式丢掉，空行压掉"""
    markup = """
    <html><head><style>.a{color:red}</style></head>
    <body><h1>标题</h1>
    <script>alert(1)</script>
    <p>第一段</p><p>第二段</p>
    </body></html>
    """
    text = html_to_text(markup)

    assert "标题" in text and "第一段" in text
    assert "alert(1)" not in text and "color:red" not in text
    assert "\n\n\n" not in text


async def test_private_hosts_are_rejected():
    """内网地址一律不让抓（防 SSRF）"""
    for url in (
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://192.168.1.1/",
        "http://10.0.0.5/",
    ):
        with pytest.raises(ValueError):
            await _assert_public(url)


def test_inherited_tools_are_discovered():
    """场景继承 WebTools，工具也要能被发现"""

    class _Scene(WebTools):
        pass

    scene = _Scene()
    names = {tool["name"] for tool in tools_of(scene)}

    assert {"web_search", "fetch_url"} <= names


# ---------- 知识库（三层查找的第一层） ----------


def test_kb_notes_and_recalls_similar_queries(tmp_path):
    """查过的东西存下来，相近的问法能对上"""
    from gensokyoai.tools import KnowledgeBase

    kb = KnowledgeBase(tmp_path)
    kb.note(
        "觉的能力",
        [{"title": "古明地觉", "url": "https://thwiki.cc/觉", "snippet": "读心"}],
    )

    assert kb.size == 1
    hits = kb.recall("觉有什么能力")
    assert hits and hits[0]["url"].endswith("/觉")
    assert hits[0]["from_query"] == "觉的能力"


def test_kb_misses_unrelated_queries(tmp_path):
    """不相干的查询不该命中"""
    from gensokyoai.tools import KnowledgeBase

    kb = KnowledgeBase(tmp_path)
    kb.note("觉的能力", [{"title": "x", "url": "https://a.example/1", "snippet": ""}])

    assert kb.recall("今晚吃什么") == []


def test_kb_room_scope_isolates_files(tmp_path):
    """按群隔离时各用各的文件"""
    from gensokyoai.tools import KnowledgeBase

    one = KnowledgeBase(tmp_path, scope="room", room="g1")
    two = KnowledgeBase(tmp_path, scope="room", room="g2")
    one.note("觉的能力", [{"title": "x", "url": "https://a.example/1", "snippet": ""}])

    assert one.path != two.path
    assert two.recall("觉的能力") == [], "别的群看不到"


def test_kb_global_scope_shares(tmp_path):
    """global 作用域下所有群共用一个文件"""
    from gensokyoai.tools import KnowledgeBase

    one = KnowledgeBase(tmp_path, scope="global", room="g1")
    two = KnowledgeBase(tmp_path, scope="global", room="g2")
    one.note("觉的能力", [{"title": "x", "url": "https://a.example/1", "snippet": ""}])

    assert one.path == two.path
    assert two.recall("觉的能力")


def test_kb_survives_broken_lines(tmp_path):
    """坏行跳过，别让整库读不出来"""
    from gensokyoai.tools import KnowledgeBase

    kb = KnowledgeBase(tmp_path)
    kb.note("测试", [{"title": "x", "url": "https://a.example/1", "snippet": ""}])
    kb.path.write_text(kb.path.read_text(encoding="utf-8") + "坏行\n", encoding="utf-8")

    assert kb.recall("测试")


async def test_lookup_uses_cache_before_searching(tmp_path):
    """第二次问同样的东西：走知识库，不再搜"""
    from gensokyoai.tools.web import WebTools

    tools = WebTools(kb_root=str(tmp_path))
    calls = []

    async def fake_search(query, limit=8):
        calls.append(query)
        return [
            {
                "title": "古明地觉 - THBWiki",
                "url": "https://thbwiki.cc/古明地觉",
                "snippet": "读心",
            }
        ]

    tools.web.search = fake_search  # type: ignore[method-assign]

    first = await tools.lookup("古明地觉")
    second = await tools.lookup("古明地觉")

    assert len(calls) == 1, "第二次不该再搜"
    assert "THBWiki" in first[0]["text"]
    assert "知识库里存过" in second[0]["text"]


async def test_lookup_prefers_authoritative_sites(tmp_path):
    """权威站命中时优先用它，并把结果写进知识库"""
    from gensokyoai.tools.web import WebTools

    tools = WebTools(kb_root=str(tmp_path))
    tools.web.search_sites = ToolSites(prefer=("thbwiki.cc",))

    async def fake_search(query, limit=8):
        return [
            {"title": "随便什么", "url": "https://spam.example/x", "snippet": ""},
            {"title": "THBWiki", "url": "https://thbwiki.cc/x", "snippet": "权威"},
        ]

    tools.web.search = fake_search  # type: ignore[method-assign]

    reply = await tools.lookup("觉")

    assert "权威站" in reply[0]["text"]
    assert "spam.example" not in reply[0]["text"], "有权威站就不给别的"
    assert tools.kb.size == 1

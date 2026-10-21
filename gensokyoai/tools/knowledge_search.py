"""knowledge_search 工具 —— 可信知识站的站内搜索（MediaWiki opensearch API）。

URL 构造收进代码：模型只给关键词，域名 / 端点 / 参数 / 编码零拼装。
真机病灶：让模型自己拼 wiki URL，它编了个不存在的 wikidream.net——
「优先用某站」这种抽象要求对发散型模型没有约束力，确定性工作归工程。

端点选型：opensearch 是 MediaWiki 最普适的搜索模块（thbwiki 的定制 api
就不实现 list=search / generator=search，实测只有 opensearch 与 titles
直查可用）。返回标题 + 真实链接（API 自报，不猜路径）；模型看中哪条
再用 fetch_url 抓正文。两种响应方言都认：标准位置数组与 thbwiki 的对象数组。
"""

from urllib.parse import quote

import msgspec

from ..core.config import KnowledgeSite
from ..schemas.model_schema import ToolSpec
from ..utils.logger import LoggerManager
from .fetch_url import guarded_get_text

_logger = LoggerManager.get_logger("KWIKI")

_API_TEMPLATE = "https://{site}/api.php?action=opensearch&search={query}&limit={limit}&format=json"
""" MediaWiki opensearch：普适性最好的站内搜索端点 """

_MAX_RESULTS_LIMIT = 10
""" 单次搜索返回条数上限 """


def _parse_hits(site: str, payload: list) -> list[tuple[str, str, str]]:
    """把两种 opensearch 响应方言归一成 (site, 标题, 链接) 列表。

    - thbwiki 方言：[{"text": 标题, "link": 链接}, ...]
    - 标准方言：[搜索词, [标题...], [描述...], [链接...]]（位置数组）
    """
    hits: list[tuple[str, str, str]] = []
    if all(isinstance(item, dict) for item in payload):
        for item in payload:
            title, link = str(item.get("text", "")), str(item.get("link", ""))
            if title and link:
                hits.append((site, title, link))
        return hits
    # 标准位置数组：payload[1]=标题 payload[3]=链接（payload[0] 是搜索词回显）
    if len(payload) == 4 and isinstance(payload[1], list) and isinstance(payload[3], list):
        for title, link in zip(payload[1], payload[3], strict=False):
            if title and link:
                hits.append((site, str(title), str(link)))
    return hits


def build_knowledge_search(sites: list[KnowledgeSite]) -> ToolSpec:
    """按可信知识站配置构造 knowledge_search 工具（配置为空时不该调用本工厂）。

    Args:
        sites: config.search.knowledge_sites（域名 + 描述）

    Returns:
        ToolSpec: 闭包绑定站点表的搜索工具
    """
    site_list = [s.site for s in sites if s.site]

    async def knowledge_search(query: str, max_results: int = 5) -> str:
        """在可信知识站里搜索词条（角色/作品/设定等领域知识优先用它，不用自己拼网址）。返回编号结果（标题/页面链接）；看中哪条再用 fetch_url 抓正文；查不到再用 web_search 泛搜。

        Args:
            query: 搜索关键词（角色名、作品名、术语均可）
            max_results: 返回条数（1~10，默认 5）

        Returns:
            str: 编号词条列表；失败或空结果时返回原因说明
        """
        query = query.strip()
        if not query:
            return "搜索失败：关键词不能为空"
        limit = max(1, min(_MAX_RESULTS_LIMIT, max_results))

        async def _search_all(keyword: str) -> tuple[list[tuple[str, str, str]], list[str]]:
            hits: list[tuple[str, str, str]] = []
            failed: list[str] = []
            for site in site_list:
                api = _API_TEMPLATE.format(site=site, query=quote(keyword), limit=limit)
                try:
                    payload = msgspec.json.decode(await guarded_get_text(api), type=list)
                    hits.extend(_parse_hits(site, payload))
                except Exception as err:
                    # 单站失败不拖垮整次搜索（其余站照常），失败名单汇总给模型
                    _logger.debug(f"knowledge_search {site} 失败: {type(err).__name__}: {err}")
                    failed.append(site)
            return hits, failed

        hits, failed = await _search_all(query)

        if not hits:
            note = f"（{', '.join(failed)} 暂时访问失败）" if failed else ""
            return (
                f"没有找到与 {query!r} 相关的词条{note}。"
                "站内搜索是标题前缀匹配：去掉修饰语，用角色全名/作品名等短词重试；"
                "仍查不到再用 web_search 泛搜"
            )

        lines = [
            f"{i}. [{site}] {title}\n   {url}"
            for i, (site, title, url) in enumerate(hits[:limit], 1)
        ]
        # 结果级使用提示：小模型接力调用时容易把「问题文本」当 URL 传给 fetch_url
        # （真机病灶），在结果里明说传的是链接
        lines.append("（把上面某条链接原样传给 fetch_url，即可阅读该词条正文）")
        if failed:
            lines.append(f"（{', '.join(failed)} 本次访问失败，结果可能不全）")
        return "\n".join(lines)

    return ToolSpec(tool_func=knowledge_search)


__all__ = ["build_knowledge_search"]

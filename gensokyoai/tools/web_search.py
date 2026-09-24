"""联网搜索工具 —— DuckDuckGo（ddgs）。

可选依赖：`pip install 'gensokyoai[search]'`。未安装时工具照常注册、
调用返回友好提示——角色的世界不该因为缺个搜索库而起不来。

ddgs 只有同步 API，但同步工具由 ToolExecutor 自动 `asyncio.to_thread`
下线程执行，这里保持普通同步函数即可。
"""

from ..core.registry import ToolRegistry

_MAX_RESULTS_LIMIT = 10
""" 单次搜索返回条数上限（防一次拉爆上下文）"""

_SEARCH_TIMEOUT = 20
""" 单次搜索超时（秒）"""


@ToolRegistry.tool
def web_search(query: str, max_results: int = 5) -> str:
    """联网搜索最新信息（DuckDuckGo）。需要查证时事、资料、冷门知识时使用。

    Args:
        query: 搜索关键词（自然语言或关键词均可）
        max_results: 返回条数（1~10，默认 5）

    Returns:
        str: 编号结果列表（标题 / 摘要 / 链接）；失败时返回原因说明
    """
    try:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException
    except ImportError:
        return "联网搜索不可用：未安装 ddgs（pip install 'gensokyoai[search]'）"

    limit = max(1, min(_MAX_RESULTS_LIMIT, max_results))
    try:
        results = DDGS(timeout=_SEARCH_TIMEOUT).text(
            query, region="wt-wt", safesearch="on", max_results=limit
        )
    except DDGSException as err:
        return f"搜索失败：{err}"
    except Exception as err:  # 网络抖动等非 ddgs 异常也不许炸主链路
        return f"搜索失败（网络异常）：{err}"

    items = [r for r in results if r.get("href")]
    if not items:
        return f"没有找到与 {query!r} 相关的结果"

    lines = []
    for i, item in enumerate(items, 1):
        lines.append(f"{i}. {item.get('title', '')}\n   {item.get('body', '')}\n   {item['href']}")
    return "\n".join(lines)

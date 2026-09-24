"""fetch_url 工具：抓取指定 URL 的正文（aiohttp 异步 + SSRF 校验 + 截断）。

定位（沿用老项目的资料策略）：查角色 / 作品 / 设定等领域知识时，优先抓
脑内【可信知识站】表里的站点页面；与领域无关的泛搜索才走 web_search。

异步实现：抓取期间不阻塞事件循环（同步工具才需要 to_thread 下线程）。
技术细节（异常栈）只进日志，给模型的只有干净的人话。
"""

import html
import re

import aiohttp

from ..core.registry import ToolRegistry
from ..utils.logger import LoggerManager
from ..utils.url_security import UnsafeUrlError, validate_external_url

_logger = LoggerManager.get_logger("FETCH")

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
""" 单次抓取超时 """

_MAX_BYTES = 1_048_576
""" 响应读取上限（1 MB）"""

_MAX_OUTPUT_CHARS = 4000
""" 给模型的正文上限（防一次抓取打爆上下文） """

_USER_AGENT = {"User-Agent": "gensokyoai fetch_url/1.0"}

_SCRIPT_STYLE_PATTERN = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """粗粒度 HTML -> 纯文本（剥 script/style + 标签替空格 + 实体反转义）"""
    text = _SCRIPT_STYLE_PATTERN.sub(" ", text)
    text = _TAG_PATTERN.sub(" ", text)
    return html.unescape(text)


@ToolRegistry.tool
async def fetch_url(url: str) -> str:
    """抓取指定 URL 的网页正文（自动剥 HTML、截断到 4000 字）。查角色/作品/设定等领域知识时，优先抓可信知识站里的页面；与领域无关的泛搜索请用 web_search。

    Args:
        url: 完整网址（http/https）

    Returns:
        str: 正文文本（超长截断）；失败时返回原因说明
    """
    url = url.strip()
    if not url:
        return "URL 不能为空"
    try:
        validate_external_url(url)
    except UnsafeUrlError as err:
        return f"这个地址不允许访问（{err.reason}）"

    try:
        async with (
            aiohttp.ClientSession(timeout=_FETCH_TIMEOUT, headers=_USER_AGENT) as session,
            session.get(url, allow_redirects=True) as response,
        ):
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            raw = await response.content.read(_MAX_BYTES + 1)
    except Exception as err:
        _logger.debug(f"fetch_url 抓取失败（{url[:80]}）: {err}")
        return f"抓取失败（{type(err).__name__}），稍后再试"

    text = raw.decode("utf-8", "replace")
    if "html" in content_type.lower():
        text = _strip_html(text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if not 200 <= status < 300:
        return f"抓取失败：HTTP {status}"
    truncated = len(text) > _MAX_OUTPUT_CHARS
    suffix = "\n（正文过长，已截断）" if truncated else ""
    return text[:_MAX_OUTPUT_CHARS] + suffix

"""fetch_url 工具：抓取指定 URL 的正文（aiohttp 异步 + SSRF 校验 + 截断）。

定位（沿用老项目的资料策略）：查角色 / 作品 / 设定等领域知识时，优先抓
脑内【可信知识站】表里的站点页面；与领域无关的泛搜索才走 web_search。

异步实现：抓取期间不阻塞事件循环（同步工具才需要 to_thread 下线程）。
技术细节（异常栈）只进日志，给模型的只有干净的人话。
"""

import html
import re
import socket
from urllib.parse import urljoin

import aiohttp

from ..core.registry import ToolRegistry
from ..utils.logger import LoggerManager
from ..utils.url_security import UnsafeUrlError, is_forbidden_address, validate_external_url

_logger = LoggerManager.get_logger("FETCH")

_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
""" 单次抓取超时 """

_MAX_REDIRECTS = 5
""" 重定向跟随上限（每一跳都重新过 SSRF 门禁） """

_MAX_BYTES = 1_048_576
""" 响应读取上限（1 MB）"""

_MAX_OUTPUT_CHARS = 4000
""" 给模型的正文上限（防一次抓取打爆上下文） """

_USER_AGENT = {"User-Agent": "gensokyoai fetch_url/1.0"}

_SCRIPT_STYLE_PATTERN = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


class _GuardedResolver(aiohttp.ThreadedResolver):
    """连接层 SSRF 复检：DNS 解析结果落在禁止网段的一律丢弃。

    URL 门禁只看字面主机名；公网域名解析到 127.0.0.1 / 云元数据（泛解析域、
    DNS rebinding）要在这里拦——这是实际建连用的那次解析，没有检查与使用的时间差。
    """

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list:
        """解析并过滤禁止地址；全部被过滤时抛 UnsafeUrlError。

        Args:
            host: 主机名
            port: 端口
            family: 地址族

        Returns:
            list: 过滤后的解析结果

        Raises:
            UnsafeUrlError: 解析结果全部落在禁止网段
        """
        results = await super().resolve(host, port, family)
        allowed = [r for r in results if not is_forbidden_address(r["host"])]
        if not allowed:
            raise UnsafeUrlError(host, "域名解析到了不允许访问的地址")
        return allowed


async def _read_capped(stream, limit: int) -> bytes:
    """读到 EOF 或上限为止。

    `StreamReader.read(n)` 只返回**当前已缓冲**的数据（最多 n 字节），不会等满——
    一次调用通常只拿到第一个网络分块（几 KB 的 <head> 与脚本），剥完 HTML 几乎
    什么都不剩，知识站查询就这样无声地返回空页。

    Args:
        stream: 响应体流（aiohttp StreamReader）
        limit: 最多读取的字节数

    Returns:
        bytes: 响应体（超过上限的部分丢弃）
    """
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = await stream.read(limit - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def guarded_get_text(url: str, *, timeout_s: float = 20.0) -> str:
    """经 SSRF 门禁的 GET（字面校验 + DNS 复检），返回解码文本。

    给站内 API 调用（knowledge_search 等）复用的最小件：不跟重定向（API 端点
    不跳转，3xx 算失败）、不剥 HTML。页面抓取请用 fetch_url 工具本体。

    Args:
        url: 完整网址（http/https）
        timeout_s: 总超时秒数

    Returns:
        str: 响应体文本（utf-8，容错解码）

    Raises:
        UnsafeUrlError: 门禁拦截
        aiohttp.ClientError / TimeoutError: 网络层失败（调用方自行翻译成人话）
    """
    validate_external_url(url)
    connector = aiohttp.TCPConnector(resolver=_GuardedResolver())
    async with (
        aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_s),
            headers=_USER_AGENT,
            connector=connector,
        ) as session,
        session.get(url, allow_redirects=False) as response,
    ):
        if not 200 <= response.status < 300:
            raise aiohttp.ClientError(f"HTTP {response.status}")
        raw = await _read_capped(response.content, _MAX_BYTES)
    return raw.decode("utf-8", "replace")


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

    # 手动跟随重定向：每一跳都重新过 SSRF 门禁（302 到内网 / 云元数据是经典绕过）
    current = url
    try:
        connector = aiohttp.TCPConnector(resolver=_GuardedResolver())
        async with aiohttp.ClientSession(
            timeout=_FETCH_TIMEOUT, headers=_USER_AGENT, connector=connector
        ) as session:
            for _ in range(_MAX_REDIRECTS + 1):
                try:
                    validate_external_url(current)
                except UnsafeUrlError as err:
                    _logger.info(f"fetch_url 拦截（{url[:80]} -> {current[:80]}）: {err.reason}")
                    return f"这个地址不允许访问（{err.reason}）"

                try:
                    async with session.get(current, allow_redirects=False) as response:
                        status = response.status
                        if status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                return f"抓取失败：HTTP {status}（重定向无目标）"
                            current = urljoin(current, location)
                            continue
                        content_type = response.headers.get("Content-Type", "")
                        raw = await _read_capped(response.content, _MAX_BYTES)
                        break
                except UnsafeUrlError:
                    raise
                except Exception as err:
                    _logger.debug(f"fetch_url 抓取失败（{current[:80]}）: {err}")
                    return f"抓取失败（{type(err).__name__}），稍后再试"
            else:
                return f"抓取失败：重定向次数超过 {_MAX_REDIRECTS} 次"
    except UnsafeUrlError as err:
        return f"这个地址不允许访问（{err.reason}）"

    text = raw.decode("utf-8", "replace")
    if "html" in content_type.lower():
        text = _strip_html(text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if not 200 <= status < 300:
        return f"抓取失败：HTTP {status}"
    truncated = len(text) > _MAX_OUTPUT_CHARS
    suffix = "\n（正文过长，已截断）" if truncated else ""
    return text[:_MAX_OUTPUT_CHARS] + suffix

"""适配器接口 —— 一个厂商一个实现，引擎只调用 `complete`。"""

from typing import Protocol

import aiohttp

from .billing import Balance
from .types import Completion, Turn


class ModelError(RuntimeError):
    """端点回了错误状态码（带上状态码与响应体，否则排查时只能靠猜）。"""

    def __init__(self, status: int, body: str, url: str = "") -> None:
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status}{' ' + url if url else ''}: {body[:300]}")

    @property
    def retryable(self) -> bool:
        """429 与 5xx 值得重试；其余 4xx 是请求本身有问题，重试没用。"""
        return self.status == 429 or self.status >= 500


async def read_error(response: aiohttp.ClientResponse, url: str = "") -> str:
    """把错误响应的正文读出来（原样给日志和用户看）。"""
    try:
        body = await response.text()
    except (aiohttp.ClientError, UnicodeDecodeError):
        body = ""
    return body.strip() or response.reason or ""


class Provider(Protocol):
    """一家模型厂商的最小接口。"""

    name: str
    """ 厂商名（openai / anthropic / ...） """

    async def complete(
        self,
        turns: list[Turn],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> Completion:
        """跑一次补全。"""
        ...

    async def aclose(self) -> None:
        """关连接。"""
        ...

    async def balance(self) -> Balance | None:
        """查账户余额（不支持的厂商返回 None）。"""
        ...


class HttpProvider:
    """共用一份长驻 HTTP 会话的适配器基类。"""

    name = "http"

    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        """懒建并复用会话（要在运行中的事件循环里创建）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def aclose(self) -> None:
        """关闭会话（可重复调用）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def balance(self) -> Balance | None:
        """查账户余额；默认认为这家没提供，子类有接口就覆盖。"""
        return None


def text_of(parts) -> str:
    """把内容块里的文字拼起来（system 与纯文本消息用）。"""
    return "".join(part.text for part in parts if hasattr(part, "text"))

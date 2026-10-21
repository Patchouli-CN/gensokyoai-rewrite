"""适配器接口 —— 一个厂商一个实现，引擎只调用 `complete`。"""

from typing import Protocol

import aiohttp

from .types import Completion, Turn


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


def text_of(parts) -> str:
    """把内容块里的文字拼起来（system 与纯文本消息用）。"""
    return "".join(part.text for part in parts if hasattr(part, "text"))

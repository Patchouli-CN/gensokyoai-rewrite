"""引擎看到的一个模型端点 —— 换厂商只换适配器，这里只管单轮补全与记账。"""

import msgspec

from .llm import (
    IMAGE,
    Image,
    Prices,
    Text,
    Turn,
    UsageLedger,
    build_provider,
    detect,
    normalize,
    price_for,
)
from .llm.types import ToolCall
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("MODEL")

DEFAULT_TIMEOUT_S = 120.0
""" 单次补全的总时限（秒） """

__all__ = ["DEFAULT_TIMEOUT_S", "ChatModel", "Reply", "ToolCall"]


class Reply(msgspec.Struct, frozen=True):
    """一次补全的结果（引擎只关心这个）。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()


class ChatModel:
    """一个聊天模型端点：厂商无关的输入，统一的结果与账目。"""

    def __init__(
        self,
        base_url: str,
        *,
        provider: str = "openai",
        name: str = "",
        api_key: str = "",
        headers: dict | None = None,
        think: bool | None = False,
        timeout: float = DEFAULT_TIMEOUT_S,
        extra: dict | None = None,
        prices: Prices | None = None,
        ledger: UsageLedger | None = None,
        capabilities=None,
        health=None,
    ) -> None:
        """初始化。

        Args:
            base_url: 端点地址（llama-server 的 /v1、各家的 API 根路径）
            provider: 厂商名（openai / anthropic）
            name: 模型名（云端必填，llama-server 可空）
            api_key: 令牌（空 = 不带鉴权头）
            headers: 额外请求头
            think: 思考开关；None = 连这个厂商专有字段都不发
            timeout: 单次补全时限（秒）
            extra: 原样并进请求体的厂商专有参数
            prices: 价格（None = 按模型名查内置表）
            ledger: 共用的用量账本（None = 自建一个只算钱的）
            capabilities: 能力声明（None = text + image）
            health: 健康中心（None = 不记账）
        """
        kind = provider.lower()
        kwargs = {
            "model": name,
            "api_key": api_key,
            "headers": headers,
            "extra": extra,
            "timeout": timeout,
        }
        if kind == "openai":
            kwargs["think"] = think
        self._provider = build_provider(kind, base_url, **kwargs)
        self.name = name or kind
        self.think = think
        self.capabilities = normalize(capabilities or ("text", "image"))
        """ 这个模型能干哪些活（看得了图、听得了声） """
        self._endpoint = base_url
        self._api_key = api_key
        self._headers = headers
        self.ledger = ledger or UsageLedger(prices or price_for(name))
        self._health = health

    async def refresh_capabilities(self) -> frozenset[str]:
        """问端点要一次能力；问不到就保持现有声明。"""
        found = await detect(
            self._endpoint,
            model=self.name,
            api_key=self._api_key,
            headers=self._headers,
        )
        if found:
            self.capabilities = found
        return self.capabilities

    async def chat(
        self,
        system: str,
        user: str,
        *,
        tools: list[dict] | None = None,
        images: list[tuple[str, str]] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> Reply:
        """单轮补全。

        Args:
            system: 系统提示（协议 + 人设）
            user: 这一轮的输入（一般是 JSON）
            tools: 原生工具声明（None = 不给工具）
            images: 附带的图片 `[(说明, data URL), ...]`（None = 纯文本）
            max_tokens: 生成上限
            temperature: 采样温度

        Returns:
            Reply: 正文与工具调用

        Raises:
            aiohttp.ClientError: 连接或 HTTP 失败
            TimeoutError: 超过 timeout
            RuntimeError: 响应不是预期的形状
        """
        parts: list = [Text(user)]
        if images and IMAGE not in self.capabilities:
            _logger.debug(
                f"{self.name or '模型'} 不支持看图，"
                f"{len(images)} 张图降级成正文里的标记"
            )
            images = None
        for label, url in images or ():
            parts.append(Image(url=url, label=label))
        turns = [
            Turn(role="system", parts=(Text(system),)),
            Turn(role="user", parts=tuple(parts)),
        ]

        completion = await self._provider.complete(
            turns,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self._record(completion)
        return Reply(text=completion.text, calls=completion.calls)

    def _record(self, completion) -> None:
        """记账：用量、花费、截断告警。"""
        self.ledger.record(completion.usage, completion.model or self.name)
        if completion.finish_reason in ("length", "max_tokens"):
            _logger.warning("模型被 max_tokens 截断，这一轮的动作可能不完整")
        if self._health is None:
            return
        self._health.count("llm.prompt_tokens", completion.usage.prompt_tokens)
        self._health.count("llm.completion_tokens", completion.usage.completion_tokens)
        self._health.observe("llm.cost_usd", self.ledger.cost)

    async def close(self) -> None:
        """关闭长驻连接（可重复调用）。"""
        await self._provider.aclose()

    async def __aenter__(self) -> "ChatModel":
        """进入上下文。"""
        return self

    async def __aexit__(self, *exc_info) -> None:
        """退出时关闭连接。"""
        await self.close()

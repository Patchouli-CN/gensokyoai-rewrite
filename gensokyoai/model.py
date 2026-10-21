"""最小模型客户端 —— OpenAI 兼容 /v1/chat/completions（llama-server 直连）。

支持原生工具调用：请求带 tools，响应里的 tool_calls 解析成结构化的动作。
整个进程复用一个 ClientSession，每次补全都有总时限。
"""

import json

import aiohttp
import msgspec

from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("MODEL")

DEFAULT_TIMEOUT_S = 120.0
""" 单次补全的总时限（秒） """


class ToolCall(msgspec.Struct, frozen=True):
    """模型发起的一次工具调用。"""

    name: str
    arguments: dict = msgspec.field(default_factory=dict)


class Reply(msgspec.Struct, frozen=True):
    """一次补全的结果：正文 + 工具调用。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()


class ChatModel:
    """一个聊天模型端点。"""

    def __init__(
        self,
        base_url: str,
        *,
        think: bool = False,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """初始化。

        Args:
            base_url: OpenAI 兼容端点（如 http://127.0.0.1:8080/v1）
            think: 模板思考开关（开着会吃光 max_tokens，工具调用基本必坏）
            timeout: 单次补全的总时限（秒）
        """
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._think = think
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _ensure_session(self) -> aiohttp.ClientSession:
        """懒建并复用长驻会话（会话需在运行中的事件循环里创建）。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

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
            RuntimeError: 响应不是预期的 chat completion 形状
        """
        content: object = user
        if images:
            parts: list[dict] = [{"type": "text", "text": user}]
            for label, url in images:
                parts.append({"type": "text", "text": label})
                parts.append({"type": "image_url", "image_url": {"url": url}})
            content = parts
        payload: dict = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {
                "thinking": self._think,
                "enable_thinking": self._think,
            },
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        session = self._ensure_session()
        async with session.post(self._url, json=payload) as resp:
            resp.raise_for_status()
            data = await resp.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as err:
            raise RuntimeError(f"模型响应形状异常: {data!r:.200}") from err
        if choice.get("finish_reason") == "length":
            _logger.warning("模型被 max_tokens 截断，这一轮的动作可能不完整")
        return Reply(
            text=(message.get("content") or "").strip(),
            calls=_calls_of(message.get("tool_calls")),
        )

    async def close(self) -> None:
        """关闭长驻会话（可重复调用）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "ChatModel":
        """进入上下文。"""
        return self

    async def __aexit__(self, *exc_info) -> None:
        """退出时关闭会话。"""
        await self.close()


def _calls_of(raw) -> tuple[ToolCall, ...]:
    """把响应里的 tool_calls 解成结构化动作；坏参数当空参处理。"""
    calls: list[ToolCall] = []
    for item in raw or ():
        function = item.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        calls.append(
            ToolCall(
                name=name, arguments=arguments if isinstance(arguments, dict) else {}
            )
        )
    return tuple(calls)

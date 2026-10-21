"""OpenAI 兼容适配器 —— llama-server / vLLM / Ollama / 各家云 API 都是这一套。"""

import json

from .base import HttpProvider, text_of
from .billing import Billing, FlatBilling, Rates
from .types import Completion, Image, Text, ToolCall, Turn, Usage

_TABLE: dict[str, Rates] = {
    "gpt-4o-mini": Rates(input=0.15, output=0.60),
    "gpt-4o": Rates(input=2.50, output=10.00),
}
""" OpenAI 官方价（USD / 百万 token） """


def billing_for(model: str) -> Billing | None:
    """按模型名找官方价；表里没有就返回 None（当成不支持计费）。"""
    hits = [key for key in _TABLE if model and model.lower().startswith(key.lower())]
    if not hits:
        return None
    key = max(hits, key=len)
    return FlatBilling(_TABLE[key], currency="USD", label=key)


def _arguments(raw) -> dict:
    """工具参数统一成 dict（有的厂商给字符串，有的直接给对象）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _calls_of(raw) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for item in raw or ():
        function = item.get("function") or {}
        name = function.get("name")
        if isinstance(name, str) and name:
            calls.append(
                ToolCall(name=name, arguments=_arguments(function.get("arguments")))
            )
    return tuple(calls)


class OpenAIProvider(HttpProvider):
    """POST /chat/completions（OpenAI 形状）。"""

    name = "openai"

    def __init__(
        self,
        base_url: str,
        *,
        model: str = "",
        api_key: str = "",
        headers: dict | None = None,
        think: bool | None = False,
        extra: dict | None = None,
        timeout: float = 120.0,
        billing: Billing | None = None,
    ) -> None:
        """初始化。

        Args:
            base_url: 端点（http://127.0.0.1:8080/v1）
            model: 模型名（llama-server 可空，云端必填）
            api_key: 令牌（空 = 不带 Authorization）
            headers: 额外请求头
            think: 是否开思考；None = 不发这个厂商专有字段
            extra: 原样并进请求体（厂商专有参数）
            timeout: 单次补全时限（秒）
            billing: 计费口径（None = 按模型名查官方价，查不到就算不支持）
        """
        super().__init__(timeout)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._headers = {"Content-Type": "application/json", **(headers or {})}
        if api_key:
            self._headers.setdefault("Authorization", f"Bearer {api_key}")
        self._think = think
        self._extra = extra or {}
        self.billing = billing if billing is not None else billing_for(model)
        """ 计费口径（None = 这家不给价，只记 token） """

    def _content(self, turn: Turn):
        """内容块 -> OpenAI 的 content（纯文字给字符串，带图给数组）。"""
        has_image = any(isinstance(part, Image) for part in turn.parts)
        if not has_image:
            return text_of(turn.parts)
        blocks: list[dict] = []
        for part in turn.parts:
            if isinstance(part, Text):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, Image):
                if part.label:
                    blocks.append({"type": "text", "text": part.label})
                blocks.append({"type": "image_url", "image_url": {"url": part.url}})
        return blocks

    def _wire(self, turn: Turn) -> dict:
        """一条消息 -> OpenAI 的消息。"""
        if turn.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": turn.tool_call_id,
                "content": text_of(turn.parts),
            }
        message: dict = {"role": turn.role, "content": self._content(turn)}
        if turn.tool_calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for index, call in enumerate(turn.tool_calls)
            ]
        return message

    async def complete(
        self,
        turns: list[Turn],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> Completion:
        """跑一次补全。"""
        payload: dict = {
            "messages": [self._wire(turn) for turn in turns],
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self._extra,
        }
        if self._model:
            payload["model"] = self._model
        if self._think is not None:
            payload["chat_template_kwargs"] = {
                "thinking": self._think,
                "enable_thinking": self._think,
            }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with self.session().post(
            self._url, json=payload, headers=self._headers
        ) as response:
            response.raise_for_status()
            data = await response.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as err:
            raise RuntimeError(f"模型响应形状异常: {data!r:.200}") from err

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return Completion(
            text=(message.get("content") or "").strip(),
            calls=_calls_of(message.get("tool_calls")),
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cached_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                ),
                reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            ),
            model=str(data.get("model") or self._model),
            finish_reason=str(choice.get("finish_reason") or ""),
        )

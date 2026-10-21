"""Anthropic 适配器 —— POST /messages（工具与图片的块结构都跟 OpenAI 不一样）。"""

from .base import HttpProvider, text_of
from .types import Completion, Image, Text, ToolCall, Turn, Usage

_API_VERSION = "2023-06-01"


def _split_data_url(url: str) -> tuple[str, str]:
    """`data:image/png;base64,AAAA` -> (media_type, 数据)。"""
    head, _, data = url.partition(",")
    media = head.removeprefix("data:").split(";")[0] or "image/png"
    return media, data


def _image_block(part: Image) -> dict:
    """一张图 -> Anthropic 的 image 块（base64 或远端 URL）。"""
    if part.url.startswith("data:"):
        media, data = _split_data_url(part.url)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": part.url}}


def _tools_of(tools: list[dict] | None) -> list[dict]:
    """把 OpenAI 形状的工具声明翻成 Anthropic 的。"""
    found = []
    for item in tools or ():
        function = item.get("function") if "function" in item else item
        found.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object"},
            }
        )
    return found


class AnthropicProvider(HttpProvider):
    """POST /messages（Claude）。"""

    name = "anthropic"

    def __init__(
        self,
        base_url: str = "https://api.anthropic.com/v1",
        *,
        model: str = "claude-sonnet-4-20250514",
        api_key: str = "",
        headers: dict | None = None,
        extra: dict | None = None,
        timeout: float = 120.0,
    ) -> None:
        """初始化。

        Args:
            base_url: 端点（https://api.anthropic.com/v1）
            model: 模型名
            api_key: 令牌
            headers: 额外请求头
            extra: 原样并进请求体
            timeout: 单次补全时限（秒）
        """
        super().__init__(timeout)
        self._url = f"{base_url.rstrip('/')}/messages"
        self._model = model
        self._headers = {
            "Content-Type": "application/json",
            "anthropic-version": _API_VERSION,
            **(headers or {}),
        }
        if api_key:
            self._headers.setdefault("x-api-key", api_key)
        self._extra = extra or {}

    def _blocks(self, turn: Turn) -> list[dict]:
        """内容块 -> Anthropic 的 content 数组。"""
        blocks: list[dict] = []
        for part in turn.parts:
            if isinstance(part, Text):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, Image):
                if part.label:
                    blocks.append({"type": "text", "text": part.label})
                blocks.append(_image_block(part))
        if turn.role == "tool":
            return [
                {
                    "type": "tool_result",
                    "tool_use_id": turn.tool_call_id,
                    "content": text_of(turn.parts),
                }
            ]
        for call in turn.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{call.name}_{len(blocks)}",
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        return blocks

    async def complete(
        self,
        turns: list[Turn],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 512,
        temperature: float = 0.8,
    ) -> Completion:
        """跑一次补全。"""
        system = "\n".join(
            text_of(turn.parts) for turn in turns if turn.role == "system"
        ).strip()
        messages = [
            {"role": turn.role, "content": self._blocks(turn)}
            for turn in turns
            if turn.role in ("user", "assistant", "tool")
        ]
        # Anthropic 只收 user / assistant；tool_result 要挂在 user 轮里
        normalized: list[dict] = []
        for message in messages:
            role = "user" if message["role"] == "tool" else message["role"]
            if normalized and normalized[-1]["role"] == role:
                normalized[-1]["content"].extend(message["content"])
            else:
                normalized.append({"role": role, "content": list(message["content"])})

        payload: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": min(max(temperature, 0.0), 1.0),
            "messages": normalized,
            **self._extra,
        }
        if system:
            payload["system"] = system
        declared = _tools_of(tools)
        if declared:
            payload["tools"] = declared

        async with self.session().post(
            self._url, json=payload, headers=self._headers
        ) as response:
            response.raise_for_status()
            data = await response.json()
        try:
            blocks = data["content"]
        except (KeyError, TypeError) as err:
            raise RuntimeError(f"模型响应形状异常: {data!r:.200}") from err

        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in blocks:
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        name=str(block.get("name") or ""),
                        arguments=block.get("input") or {},
                    )
                )
        usage = data.get("usage") or {}
        return Completion(
            text="".join(texts).strip(),
            calls=tuple(call for call in calls if call.name),
            usage=Usage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
            ),
            model=str(data.get("model") or self._model),
            finish_reason=str(data.get("stop_reason") or ""),
        )

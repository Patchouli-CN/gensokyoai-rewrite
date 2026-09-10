"""模型提供者基类 + OpenAI 兼容 HTTP 通用实现"""

import time
from abc import ABC, abstractmethod
from typing import Self

import aiohttp
import msgspec

from ..core.toolkit import build_executor
from ..schemas.model_schema import (
    CompletionResult,
    Message,
    ModelConfig,
    StreamEvent,
    ToolCall,
    ToolSpec,
    Usage,
)
from ..utils.logger import LoggerManager

_MAX_TOOL_ROUNDS = 8
""" 单次 chat 内工具执行轮数上限，防止模型无限循环调工具 """


def _preview(text: str, limit: int = 80) -> str:
    """日志预览：长文本截断并标注总长，换行折叠。"""
    flat = text.replace("\n", "\\n")
    if len(flat) <= limit:
        return f"{flat!r} ({len(text)}字)"
    return f"{flat[:limit]!r}... (共{len(text)}字)"


def to_openai_messages(messages: list[Message]) -> list[dict]:
    """序列化内部消息为 OpenAI 兼容字典列表。"""
    out: list[dict] = []
    for m in messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.name is not None:
            d["name"] = m.name
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        if m.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in m.tool_calls
            ]
        out.append(d)
    return out


def split_think(content: str) -> tuple[str, str | None]:
    """分离回复中的 <think> 思考段与正文。"""
    if "</think>" not in content:
        return content, None
    think, _, body = content.partition("</think>")
    return body.strip(), think.replace("<think>", "").strip() or None


class ModelProvider(ABC):
    """AI模型提供商。协议保持「薄」：人设、system prompt、上下文裁剪属于上层职责。"""

    @abstractmethod
    def config(self, conf: ModelConfig) -> Self:
        """更新配置，返回自身以便链式调用"""
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult:
        """单次补全（无状态，调用方持有全部消息）"""
        ...

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """将模型原始输出中的工具调用统一为 OpenAI 标准格式。

        Args:
            result: 原始补全结果
            parsed_content: 解析后的 JSON 内容（如果是 JSON 输出）

        Returns:
            标准化后的 CompletionResult（tool_calls 字段已填充）
        """
        return result

    @property
    def supports_streaming(self) -> bool:
        """该 provider 是否支持流式投递（供投递层决定流式/缓冲）。默认 False。"""
        return False

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ):
        """流式补全：逐块产出 StreamEvent。

        基类兜底实现：直接调无状态的 `chat()` 并整段一次性产出（单块）。
        子类若支持真流式（如 OpenAI 兼容 SSE）应覆盖本方法。
        内部模块（brain/ooc/压缩）请别用本方法 —— 它们走 `chat()` 缓冲即可。

        Yields:
            StreamEvent: 正文块与末块（末块含 finish_reason / usage）
        """
        result = await self.chat(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            tools=tools,
        )
        yield StreamEvent(
            delta=result.content, finish_reason=result.finish_reason, usage=result.usage
        )


class OpenAICompatProvider(ModelProvider):
    """OpenAI 兼容 /v1/chat/completions HTTP 基类（llama-server / vLLM / 兼容网关）。"""

    log_tag = "MODEL"

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger(self.log_tag)
        self._conf: ModelConfig | None = None

    def config(self, conf: ModelConfig) -> Self:
        """更新配置。"""
        self._conf = conf
        self._logger.info(f"模型配置更新: {conf.model_name} @ {conf.base_url}")
        return self

    async def chat(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> CompletionResult:
        """单次补全：POST {base_url}/chat/completions。"""
        if self._conf is None:
            raise RuntimeError("模型未配置: 请先调用 config()")
        if self._conf.invoker_type != "openai":
            raise NotImplementedError(f"调用模式 {self._conf.invoker_type} 暂未实现")

        headers = {"Content-Type": "application/json"}
        if self._conf.token:
            headers["Authorization"] = f"Bearer {self._conf.token}"

        url = f"{self._conf.base_url.rstrip('/')}/chat/completions"
        self._logger.debug(
            f"请求发出: url={url} 模型={self._conf.model_name} 消息数={len(messages)} "
            f"max_tokens={max_new_tokens} temperature={temperature} stop={stop} "
            f"流式=False 思考={self._conf.think} 工具={len(tools) if tools else 0}个"
        )
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=self._conf.timeout)
        result: CompletionResult | None = None
        prompt_total = completion_total = cached_total = 0
        rounds = 0

        async with aiohttp.ClientSession(timeout=timeout) as http:
            while True:
                payload = self._build_payload(
                    messages,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    stop=stop,
                )
                if tools:
                    payload["tools"] = [t.to_openai_tool() for t in tools]
                    payload["tool_choice"] = "auto"
                data = await self._post_json(http, url, payload, headers, started)
                result = self._build_result(data)
                prompt_total += result.usage.prompt_tokens
                completion_total += result.usage.completion_tokens
                cached_total += result.usage.cached_tokens

                if not result.tool_calls or not tools:
                    break
                rounds += 1
                if rounds > _MAX_TOOL_ROUNDS:
                    self._logger.warning(f"工具调用超过 {_MAX_TOOL_ROUNDS} 轮，强制收尾")
                    break
                messages = await self._execute_tools(messages, result, tools)

        assert result is not None
        if rounds:
            result = CompletionResult(
                content=result.content,
                reasoning=result.reasoning,
                finish_reason=result.finish_reason,
                usage=Usage(
                    prompt_tokens=prompt_total,
                    completion_tokens=completion_total,
                    cached_tokens=cached_total,
                ),
                model=result.model,
            )

        elapsed = time.monotonic() - started
        self._logger.info(
            f"补全完成: {prompt_total}+{completion_total} tok "
            f"finish={result.finish_reason} 工具轮数={rounds} 耗时={elapsed:.2f}s 速度="
            f"{completion_total / elapsed:.1f}tok/s 前缀缓存命中={cached_total}/{prompt_total} tok"
        )
        self._logger.debug(
            f"补全详情: 正文={_preview(result.content)} "
            f"思考段={len(result.reasoning or '')}字 模型={result.model} "
            f"工具调用={len(result.tool_calls) if result.tool_calls else 0}个"
        )
        return result

    @property
    def supports_streaming(self) -> bool:
        """OpenAI 兼容协议是否允许流式投递：由配置 `streaming` 决定。"""
        return bool(self._conf and self._conf.streaming)

    async def chat_stream(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int = 512,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ):
        """真流式：POST 流式请求并逐块解析 SSE，产出 StreamEvent。

        仅用于投递层（给用户看的那一条），内部模块请走 `chat()` 缓冲。
        文本生成的流式；工具调用（delta.tool_calls）暂不在流式路径内处理，
        需要工具时直接用 `chat()` 的非流式多轮循环。

        Yields:
            StreamEvent: 正文块（delta 非空）+ 末块（delta 空，附 finish_reason / usage）
        """
        if self._conf is None:
            raise RuntimeError("模型未配置: 请先调用 config()")
        if self._conf.invoker_type != "openai":
            raise NotImplementedError(f"调用模式 {self._conf.invoker_type} 暂未实现流式")

        headers = {"Content-Type": "application/json"}
        if self._conf.token:
            headers["Authorization"] = f"Bearer {self._conf.token}"

        url = f"{self._conf.base_url.rstrip('/')}/chat/completions"
        self._logger.debug(
            f"流式请求发出: url={url} 模型={self._conf.model_name} 消息数={len(messages)} "
            f"max_tokens={max_new_tokens} temperature={temperature} stop={stop} 工具={len(tools) if tools else 0}个"
        )
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=self._conf.timeout)

        payload = self._build_payload(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            stream=True,
        )
        # llama-server / vLLM 默认**不在流式响应里回传 usage**（实测：不带该选项则
        # 完全没有 usage 事件，token 计量恒为 0）。显式索取，末块才有用量可记。
        payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [t.to_openai_tool() for t in tools]
            payload["tool_choice"] = "auto"

        prompt_total = 0
        completion_total = 0
        cached_total = 0
        finish = "stop"

        async with aiohttp.ClientSession(timeout=timeout) as http:
            async for event in self._iter_sse_events(http, url, payload, headers):
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    yield StreamEvent(delta=text)
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                usage = event.get("usage")
                if usage:
                    prompt_total += int(usage.get("prompt_tokens", 0))
                    completion_total += int(usage.get("completion_tokens", 0))
                    cached_total += int(
                        (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                    )

        elapsed = time.monotonic() - started
        self._logger.info(
            f"流式完成: {prompt_total}+{completion_total} tok finish={finish} 耗时={elapsed:.2f}s "
            f"前缀缓存命中={cached_total}/{prompt_total} tok"
        )
        # 末块：delta 空串，附 finish_reason 与累计用量
        yield StreamEvent(
            delta="",
            finish_reason=finish,
            usage=Usage(
                prompt_tokens=prompt_total,
                completion_tokens=completion_total,
                cached_tokens=cached_total,
            ),
        )

    async def _iter_sse_events(
        self,
        http: aiohttp.ClientSession,
        url: str,
        payload: dict,
        headers: dict,
    ):
        """发送流式请求并逐条解析 SSE `data:` 行（测试可覆写此方法）。

        Yields:
            dict: 每条 SSE 事件解析出的 JSON 对象
        """
        async with http.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._logger.error(
                    f"流式请求失败: url={url} status={resp.status} 响应体={body[:300]!r}"
                )
            resp.raise_for_status()
            async for raw in resp.content:
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        yield msgspec.json.decode(data, type=dict)
                    except Exception:
                        self._logger.debug(f"SSE 行解析失败，跳过: {data[:120]!r}")

    async def _post_json(
        self,
        http: aiohttp.ClientSession,
        url: str,
        payload: dict,
        headers: dict,
        started: float,
    ) -> dict:
        """单次 POST 并解析 JSON 响应。"""
        async with http.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._logger.error(
                    f"请求失败: url={url} status={resp.status} "
                    f"耗时={time.monotonic() - started:.2f}s 响应体={body[:300]!r}"
                )
            resp.raise_for_status()
            return await resp.json()

    async def _execute_tools(
        self,
        messages: list[Message],
        result: CompletionResult,
        tools: list[ToolSpec],
    ) -> list[Message]:
        """执行模型请求的工具调用，回填 assistant + tool 消息。

        执行本体统一走 `ToolExecutor`（结果截断 / 超时 / 同步工具下线程 / 结构化错误），
        与接力思考循环共用同一份实现。
        """
        messages = [
            *messages,
            Message(role="assistant", content=result.content, tool_calls=result.tool_calls),
        ]
        calls = result.tool_calls or []
        outcomes = await build_executor(
            tools,
            timeout=getattr(self._conf, "tool_timeout", 10.0),
            max_result_chars=getattr(self._conf, "tool_max_result_chars", 2000),
        ).execute_many(calls)
        for call, outcome in zip(calls, outcomes, strict=True):
            messages.append(
                Message(role="tool", tool_call_id=call.id, content=outcome.to_model_text())
            )
        return messages

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
        stream: bool = False,
    ) -> dict:
        """构造请求体；子类可覆盖注入方言参数。

        Note:
            `stream` 仅由投递层的 `chat_stream()` 置 True（SSE 流式）；
            `chat()` 缓冲路径恒为 False，不受配置 `streaming` 影响。
        """
        payload: dict = {
            "model": self._conf.model_name if self._conf else "",
            "messages": to_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_new_tokens,
            "stream": stream,
        }
        if stop:
            payload["stop"] = stop
        return payload

    def _build_result(self, data: dict) -> CompletionResult:
        """解析 /chat/completions 响应；子类可覆盖定制。"""
        choice = data["choices"][0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        tool_calls = [
            ToolCall(
                id=str(c.get("id", "")),
                name=str((c.get("function") or {}).get("name", "")),
                arguments=str((c.get("function") or {}).get("arguments") or "{}"),
            )
            for c in (message.get("tool_calls") or [])
        ] or None
        result = CompletionResult(
            content=message.get("content") or "",
            reasoning=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cached_tokens=int(
                    (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                ),
            ),
            model=data.get("model") or (self._conf.model_name if self._conf else ""),
            tool_calls=tool_calls,
        )
        return result

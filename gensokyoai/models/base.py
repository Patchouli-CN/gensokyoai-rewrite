""" 模型提供者基类 + OpenAI 兼容 HTTP 通用实现 """

import time
from typing import Self
from abc import ABC, abstractmethod

import aiohttp
import msgspec

from ..schemas.model_schema import CompletionResult, Message, ModelConfig, ToolCall, ToolSpec, Usage
from ..utils.logger import LoggerManager

_MAX_TOOL_ROUNDS = 8
""" 单次 chat 内工具执行轮数上限，防止模型无限循环调工具 """

def _preview(text: str, limit: int = 80) -> str:
    """ 日志预览：长文本截断并标注总长，换行折叠。 """
    flat = text.replace("\n", "\\n")
    if len(flat) <= limit:
        return f"{flat!r} ({len(text)}字)"
    return f"{flat[:limit]!r}... (共{len(text)}字)"

def to_openai_messages(messages: list[Message]) -> list[dict]:
    """ 序列化内部消息为 OpenAI 兼容字典列表。 """
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
    """ 分离回复中的 <think> 思考段与正文。 """
    if "</think>" not in content:
        return content, None
    think, _, body = content.partition("</think>")
    return body.strip(), think.replace("<think>", "").strip() or None

class ModelProvider(ABC):
    """ AI模型提供商。协议保持「薄」：人设、system prompt、上下文裁剪属于上层职责。"""

    @abstractmethod
    def config(self, conf: ModelConfig) -> Self:
        """ 更新配置，返回自身以便链式调用 """
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
        """ 单次补全（无状态，调用方持有全部消息）"""
        ...

    def normalize_tool_calls(self, result: CompletionResult, parsed_content: dict | None = None) -> CompletionResult:
        """ 将模型原始输出中的工具调用统一为 OpenAI 标准格式。
        
        Args:
            result: 原始补全结果
            parsed_content: 解析后的 JSON 内容（如果是 JSON 输出）
            
        Returns:
            标准化后的 CompletionResult（tool_calls 字段已填充）
        """
        return result


class OpenAICompatProvider(ModelProvider):
    """ OpenAI 兼容 /v1/chat/completions HTTP 基类（llama-server / vLLM / 兼容网关）。"""

    log_tag = "MODEL"

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger(self.log_tag)
        self._conf: ModelConfig | None = None

    def config(self, conf: ModelConfig) -> Self:
        """ 更新配置。 """
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
        """ 单次补全：POST {base_url}/chat/completions。 """
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
        prompt_total = completion_total = 0
        rounds = 0

        async with aiohttp.ClientSession(timeout=timeout) as http:
            while True:
                payload = self._build_payload(
                    messages, max_new_tokens=max_new_tokens,
                    temperature=temperature, stop=stop,
                )
                if tools:
                    payload["tools"] = [t.to_openai_tool() for t in tools]
                    payload["tool_choice"] = "auto"
                data = await self._post_json(http, url, payload, headers, started)
                result = self._build_result(data)
                prompt_total += result.usage.prompt_tokens
                completion_total += result.usage.completion_tokens

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
                usage=Usage(prompt_tokens=prompt_total, completion_tokens=completion_total),
                model=result.model,
            )

        elapsed = time.monotonic() - started
        self._logger.info(
            f"补全完成: {prompt_total}+{completion_total} tok "
            f"finish={result.finish_reason} 工具轮数={rounds} 耗时={elapsed:.2f}s 速度="
            f"{completion_total / elapsed:.1f}tok/s"
        )
        self._logger.debug(
            f"补全详情: 正文={_preview(result.content)} "
            f"思考段={len(result.reasoning or '')}字 模型={result.model} "
            f"工具调用={len(result.tool_calls) if result.tool_calls else 0}个"
        )
        return result

    async def _post_json(
        self,
        http: aiohttp.ClientSession,
        url: str,
        payload: dict,
        headers: dict,
        started: float,
    ) -> dict:
        """ 单次 POST 并解析 JSON 响应。 """
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
        """ 执行模型请求的工具调用，回填 assistant + tool 消息。 """
        by_name = {t.tool_func.__name__: t for t in tools}
        messages = [
            *messages,
            Message(role="assistant", content=result.content, tool_calls=result.tool_calls),
        ]
        for tc in result.tool_calls or []:
            tool = by_name.get(tc.name)
            try:
                args = msgspec.json.decode(tc.arguments or "{}", type=dict)
            except Exception:
                self._logger.warning(f"工具参数 JSON 解析失败: {tc.name}({tc.arguments!r})")
                args = {}

            if tool is None:
                output = f"错误: 未知工具 {tc.name}"
                self._logger.warning(f"工具调用: {output}")
            elif tool.is_async:
                output = await tool.ainvoke(**args)
            else:
                output = tool.invoke(**args)

            messages.append(Message(role="tool", tool_call_id=tc.id, content=str(output)))
            self._logger.info(f"工具调用: {tc.name}({args}) -> {str(output)[:80]!r}")
        return messages

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
    ) -> dict:
        """ 构造请求体；子类可覆盖注入方言参数。 """
        payload: dict = {
            "model": self._conf.model_name if self._conf else "",
            "messages": to_openai_messages(messages),
            "temperature": temperature,
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        return payload

    def _build_result(self, data: dict) -> CompletionResult:
        """ 解析 /chat/completions 响应；子类可覆盖定制。 """
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
            ),
            model=data.get("model") or (self._conf.model_name if self._conf else ""),
            tool_calls=tool_calls,
        )
        return result

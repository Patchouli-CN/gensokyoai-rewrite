"""模型提供者基类 + OpenAI 兼容 HTTP 通用实现"""

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Self

import aiohttp
import msgspec

from ..core.toolkit import build_executor
from ..schemas.cost_schema import CostBreakdown, cost_of
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
from ..utils.ratelimit import RATE_LIMITS
from .pricing import price_for

_MAX_TOOL_ROUNDS = 8
""" 单次 chat 内工具执行轮数上限，防止模型无限循环调工具 """


def _parse_usage(usage: dict) -> Usage:
    """解析 OpenAI 协议的 usage 块（缓冲响应与流式末块同一口径）。

    缓存命中在 `prompt_tokens_details.cached_tokens`；缓存写入各家字段不一，
    兼容 `cache_write_input_tokens`（details 内）与 Anthropic 式的
    `cache_creation_input_tokens`（顶层）。

    Args:
        usage: 响应里的 usage 字典

    Returns:
        Usage: 用量
    """
    details = usage.get("prompt_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        cached_tokens=int(details.get("cached_tokens", 0)),
        cache_write_tokens=int(
            details.get("cache_write_input_tokens", usage.get("cache_creation_input_tokens", 0))
        ),
    )


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
        execute_tools: bool = True,
    ) -> CompletionResult:
        """单次补全（无状态，调用方持有全部消息）

        execute_tools=False 时原生 tool_calls 不在 provider 内循环消化，
        原样返回给调用方（脑层要自己执行并掌控回填格式时用它）。
        """
        ...

    async def probe_native_tools(self) -> bool:
        """探测该模型是否支持原生 tool_calls；默认不支持（走文本喊话备胎）。

        支持 OpenAI 兼容协议的子类应覆盖为真探测（一次性小请求，结果缓存）。
        """
        return False

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
        self._native_probe: bool | None = None
        """ 原生 tool_calls 探测结果缓存（None = 未探测或上次只得到瞬时故障）"""
        self._probe_lock = asyncio.Lock()
        """ 探测单飞：多个频道 / 步骤同时首调时只发一次探测请求 """
        self._http: aiohttp.ClientSession | None = None
        """ 复用的 HTTP 会话（连接池）：一回合要调 4~10 次模型，每次新建会话
        = 每次重新 TCP+TLS 握手（云端模型尤其明显）。超时按请求单独给 """

    def _session(self) -> aiohttp.ClientSession:
        """取复用的 HTTP 会话；未建 / 已关 / 绑在别的事件循环上时重建。

        Returns:
            aiohttp.ClientSession: 当前事件循环上可用的会话
        """
        http = self._http
        if http is None or http.closed or http._loop is not asyncio.get_running_loop():
            http = aiohttp.ClientSession()
            self._http = http
        return http

    async def aclose(self) -> None:
        """关闭复用的 HTTP 会话（进程 / 服务退出时调用；之后再调用会自动重建）。"""
        http, self._http = self._http, None
        if http is not None and not http.closed:
            await http.close()

    async def probe_native_tools(self) -> bool:
        """启发式探测原生 tool_calls 支持：一次性小请求，结果按实例缓存。

        配置 `tool_support` 显式指定时短路（True/False 都跳过探测）。
        探测发一个「必须调用 dummy 工具」的最小请求：返回带 tool_calls = 支持；
        没调 / 4xx（服务端不认 tools 参数）= 不支持。这两种是**确定的答案**，缓存；
        超时 / 断连 / 5xx / 429 是瞬时故障（llama-server 还在加载模型时很常见），
        本次按不支持处理但**不缓存**——否则一次超时就把支持原生调用的模型永久
        锁在文本喊话备胎上，直到重启。
        """
        if self._conf is not None and self._conf.tool_support is not None:
            return self._conf.tool_support
        if self._native_probe is not None:
            return self._native_probe
        async with self._probe_lock:
            if self._native_probe is not None:
                return self._native_probe
            supported, definite = await self._probe_once()
            if definite:
                self._native_probe = supported
            self._logger.info(
                f"原生 tool_calls 探测: {'支持（原生优先）' if supported else '不支持（文本喊话备胎）'}"
                + ("" if definite else "（瞬时故障，下次重探）")
            )
            return supported

    async def _probe_once(self) -> tuple[bool, bool]:
        """发一次探测请求。

        Returns:
            tuple[bool, bool]: (是否支持, 是否为确定答案——瞬时故障为 False)
        """
        try:
            url, headers = self._request_target()
            payload = self._build_payload(
                [Message(role="user", content="调用 noop 工具")],
                max_new_tokens=64,
                temperature=0.0,
                stop=None,
            )
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": "noop",
                        "description": "空操作测试工具，必须被调用",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
            payload["tool_choice"] = "required"
            timeout = aiohttp.ClientTimeout(total=min(self._conf.timeout, 60.0))  # type: ignore[union-attr]
            data = await self._post_json(
                self._session(), url, payload, headers, time.monotonic(), timeout=timeout
            )
            return bool(self._build_result(data).tool_calls), True
        except aiohttp.ClientResponseError as err:
            definite = 400 <= err.status < 500 and err.status not in (408, 429)
            self._logger.warning(f"原生 tool_calls 探测被拒: HTTP {err.status}")
            return False, definite
        except Exception:
            self._logger.exception("原生 tool_calls 探测失败，按不支持处理（文本喊话备胎）")
            return False, False

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
        execute_tools: bool = True,
    ) -> CompletionResult:
        """单次补全：POST {base_url}/chat/completions。

        execute_tools=False 时原生 tool_calls 不进内循环、原样返回——
        脑层接力/管线用它把「原生调用」与「文本喊话」统一到同一条
        执行+回填路径（查证兜底等语义与调用通道解耦）。
        """
        if self._conf is None:
            raise RuntimeError("模型未配置: 请先调用 config()")
        if self._conf.invoker_type != "openai":
            raise NotImplementedError(f"调用模式 {self._conf.invoker_type} 暂未实现")

        url, headers = self._request_target()
        self._logger.debug(
            f"请求发出: url={url} 模型={self._conf.model_name} 消息数={len(messages)} "
            f"max_tokens={max_new_tokens} temperature={temperature} stop={stop} "
            f"流式=False 思考={self._conf.think} 工具={len(tools) if tools else 0}个"
        )
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=self._conf.timeout)
        result: CompletionResult | None = None
        prompt_total = completion_total = cached_total = cache_write_total = 0
        rounds = 0

        http = self._session()
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
            data = await self._post_json(http, url, payload, headers, started, timeout=timeout)
            result = self._build_result(data)
            prompt_total += result.usage.prompt_tokens
            completion_total += result.usage.completion_tokens
            cached_total += result.usage.cached_tokens
            cache_write_total += result.usage.cache_write_tokens

            if not result.tool_calls or not tools or not execute_tools:
                break
            rounds += 1
            if rounds > _MAX_TOOL_ROUNDS:
                self._logger.warning(f"工具调用超过 {_MAX_TOOL_ROUNDS} 轮，强制收尾")
                break
            messages = await self._execute_tools(messages, result, tools)

        assert result is not None
        if rounds:
            # replace 而非手搓构造：手搓漏掉了 tool_calls（超轮强制收尾时调用信息丢失）
            result = msgspec.structs.replace(
                result,
                usage=Usage(
                    prompt_tokens=prompt_total,
                    completion_tokens=completion_total,
                    cached_tokens=cached_total,
                    cache_write_tokens=cache_write_total,
                ),
            )

        elapsed = time.monotonic() - started
        self._logger.info(
            f"补全完成: {prompt_total}+{completion_total} tok "
            f"finish={result.finish_reason} 工具轮数={rounds} 耗时={elapsed:.2f}s 速度="
            f"{completion_total / elapsed:.1f}tok/s 前缀缓存命中={cached_total}/{prompt_total} tok"
            f"{self._cost_suffix(result.usage, result.model)}"
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

        url, headers = self._request_target()
        self._logger.debug(
            f"流式请求发出: url={url} 模型={self._conf.model_name} 消息数={len(messages)} "
            f"max_tokens={max_new_tokens} temperature={temperature} stop={stop} 工具={len(tools) if tools else 0}个"
        )
        started = time.monotonic()
        # 流式不能用 total 超时：它覆盖整个响应体读取，本地模型 10 tok/s 吐 1024 token
        # 就会在半句话处被掐断（前半句已经投递给用户了）。改为「两块之间」的空闲超时。
        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=self._conf.timeout, sock_read=self._conf.timeout
        )

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
        cache_write_total = 0
        finish = "stop"
        model_name = self._conf.model_name

        async for event in self._iter_sse_events(
            self._session(), url, payload, headers, timeout=timeout
        ):
            choice = (event.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            if text:
                yield StreamEvent(delta=text)
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            if event.get("usage"):
                usage = _parse_usage(event["usage"])
                prompt_total += usage.prompt_tokens
                completion_total += usage.completion_tokens
                cached_total += usage.cached_tokens
                cache_write_total += usage.cache_write_tokens
            if event.get("model"):
                model_name = event["model"]

        elapsed = time.monotonic() - started
        final_usage = Usage(
            prompt_tokens=prompt_total,
            completion_tokens=completion_total,
            cached_tokens=cached_total,
            cache_write_tokens=cache_write_total,
        )
        self._logger.info(
            f"流式完成: {prompt_total}+{completion_total} tok finish={finish} 耗时={elapsed:.2f}s "
            f"前缀缓存命中={cached_total}/{prompt_total} tok"
            f"{self._cost_suffix(final_usage, model_name)}"
        )
        # 末块：delta 空串，附 finish_reason 与累计用量
        yield StreamEvent(
            delta="",
            finish_reason=finish,
            usage=final_usage,
        )

    async def _iter_sse_events(
        self,
        http: aiohttp.ClientSession,
        url: str,
        payload: dict,
        headers: dict,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ):
        """发送流式请求并逐条解析 SSE `data:` 行（测试可覆写此方法）。

        Yields:
            dict: 每条 SSE 事件解析出的 JSON 对象
        """
        async with http.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._logger.error(
                    f"流式请求失败: url={url} status={resp.status} 响应体={body[:300]!r}"
                )
            resp.raise_for_status()
            self._note_rate_limits(resp.headers)
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
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict:
        """单次 POST 并解析 JSON 响应。"""
        async with http.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                self._logger.error(
                    f"请求失败: url={url} status={resp.status} "
                    f"耗时={time.monotonic() - started:.2f}s 响应体={body[:300]!r}"
                )
            resp.raise_for_status()
            self._note_rate_limits(resp.headers)
            return await resp.json()

    def _note_rate_limits(self, headers) -> None:
        """把响应头里的限流窗口信息喂给进程级注册表（/quota 的「剩余额度」数据源）。

        纯观测管道：供应商不带这些头就静默跳过，不参与任何决策。
        """
        if self._conf is not None:
            RATE_LIMITS.note(self._conf.base_url, headers)

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

    def costs(self, usage: Usage, model: str = "") -> CostBreakdown:
        """给一次调用算账：配置价 > 内置价格表；都查不到 = unpriced。

        协议约定（ChatBackend）：不实现 costs() 的后端视为不计价——
        本地 llama-server 默认就在这条路上（电费不在 token 计费口径内）。
        """
        conf = self._conf
        model_name = model or (conf.model_name if conf else "")
        price = conf.price if conf is not None else None
        if price is None:
            price = price_for(model_name)
        return cost_of(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            price=price,
            model=model_name,
        )

    def _cost_suffix(self, usage: Usage, model: str) -> str:
        """补全日志的费用后缀（unpriced 时安静返回空串）。"""
        breakdown = self.costs(usage, model)
        if not breakdown.priced:
            return ""
        return f" 费用={breakdown.currency} {breakdown.total:.6f}"

    def _request_target(self) -> tuple[str, dict[str, str]]:
        """补全端点 URL 与请求头（探测 / 缓冲 / 流式三处共用）。

        Returns:
            tuple[str, dict[str, str]]: (URL, 请求头)

        Raises:
            RuntimeError: 模型未配置
        """
        if self._conf is None:
            raise RuntimeError("模型未配置: 请先调用 config()")
        headers = {"Content-Type": "application/json"}
        if self._conf.token:
            headers["Authorization"] = f"Bearer {self._conf.token}"
        return f"{self._conf.base_url.rstrip('/')}/chat/completions", headers

    def _build_result(self, data: dict) -> CompletionResult:
        """解析 /chat/completions 响应；子类可覆盖定制。"""
        choice = data["choices"][0]
        message = choice.get("message") or {}
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
            usage=_parse_usage(data.get("usage") or {}),
            model=data.get("model") or (self._conf.model_name if self._conf else ""),
            tool_calls=tool_calls,
        )
        return result

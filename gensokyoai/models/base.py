""" 模型提供者基类 + OpenAI 兼容 HTTP 通用实现 """

import time
from typing import Self
from abc import ABC, abstractmethod

import aiohttp

from ..schemas.model_schema import CompletionResult, Message, ModelConfig, ToolSpec, Usage
from ..utils.logger import LoggerManager

def _preview(text: str, limit: int = 80) -> str:
    """ 日志预览：长文本截断并标注总长，换行折叠。

    Args:
        text: 原始文本
        limit: 预览字符上限

    Returns:
        str: 可读的日志片段，如 "'前80字...' (共1024字)"
    """
    flat = text.replace("\n", "\\n")
    if len(flat) <= limit:
        return f"{flat!r} ({len(text)}字)"
    return f"{flat[:limit]!r}... (共{len(text)}字)"

def to_openai_messages(messages: list[Message]) -> list[dict]:
    """ 序列化内部消息为 OpenAI 兼容字典列表。

    None 字段一律省略：llama-server 严格校验消息结构，
    收到 "name": null 会报 json.exception.type_error.302。

    Args:
        messages: 内部 Message 列表

    Returns:
        list[dict]: 可直接 JSON 序列化的消息字典
    """
    out: list[dict] = []
    for m in messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.name is not None:
            d["name"] = m.name
        if m.tool_call_id is not None:
            d["tool_call_id"] = m.tool_call_id
        out.append(d)
    return out

def split_think(content: str) -> tuple[str, str | None]:
    """ 分离回复中的 <think> 思考段与正文。

    llama-server 未开 reasoning_format 自动分离时的兜底；
    闭合标签优先，截断的未闭合思考段原样保留。

    Args:
        content: 模型原始输出

    Returns:
        tuple[str, str | None]: (正文, 思考内容；无思考段则为 None)
    """
    if "</think>" not in content:
        return content, None
    think, _, body = content.partition("</think>")
    return body.strip(), think.replace("<think>", "").strip() or None

class ModelProvider(ABC):
    """ AI模型提供商。

    协议保持「薄」：人设、system prompt、上下文裁剪属于 SessionManager/上层职责，
    Provider 只负责一次纯粹的补全调用。
    """

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

class OpenAICompatProvider(ModelProvider):
    """ OpenAI 兼容 /v1/chat/completions HTTP 基类（llama-server / vLLM / 兼容网关）。

    子类通过覆盖 _build_result 定制响应解析（如 llama-server 的思考段分离）。
    """

    log_tag = "MODEL"
    """ 日志模块标识，子类可覆盖 """

    def __init__(self) -> None:
        self._logger = LoggerManager.get_logger(self.log_tag)
        self._conf: ModelConfig | None = None

    def config(self, conf: ModelConfig) -> Self:
        """ 更新配置。

        Args:
            conf: 模型配置（base_url / model_name 等）

        Returns:
            self，支持链式调用
        """
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
        """ 单次补全：POST {base_url}/chat/completions。

        Args:
            messages: 完整消息列表（无状态，调用方持有）
            max_new_tokens: 最大生成长度
            temperature: 采样温度
            stop: 停止序列
            tools: 工具声明（当前未实现，传非空将报错）

        Returns:
            CompletionResult: 含回复文本与 token 用量

        Raises:
            RuntimeError: 未调用 config() 或 invoker_type 不支持
            NotImplementedError: tools 暂未实现
            aiohttp.ClientError: 网络/HTTP 错误
        """
        if self._conf is None:
            raise RuntimeError("模型未配置: 请先调用 config()")
        if self._conf.invoker_type != "openai":
            raise NotImplementedError(f"调用模式 {self._conf.invoker_type} 暂未实现")
        if tools:
            raise NotImplementedError("工具调用暂未实现")

        payload = self._build_payload(
            messages, max_new_tokens=max_new_tokens, temperature=temperature, stop=stop,
        )

        headers = {"Content-Type": "application/json"}
        if self._conf.token:
            headers["Authorization"] = f"Bearer {self._conf.token}"

        url = f"{self._conf.base_url.rstrip('/')}/chat/completions"
        self._logger.debug(
            f"请求发出: url={url} 模型={self._conf.model_name} 消息数={len(messages)} "
            f"max_tokens={max_new_tokens} temperature={temperature} stop={stop} "
            f"流式=False 思考={self._conf.think}"
        )
        started = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=self._conf.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    self._logger.error(
                        f"请求失败: url={url} status={resp.status} "
                        f"耗时={time.monotonic() - started:.2f}s 响应体={body[:300]!r}"
                    )
                resp.raise_for_status()
                data = await resp.json()

        elapsed = time.monotonic() - started
        result = self._build_result(data)
        self._logger.info(
            f"补全完成: {result.usage.prompt_tokens}+{result.usage.completion_tokens} tok "
            f"finish={result.finish_reason} 耗时={elapsed:.2f}s 速度="
            f"{result.usage.completion_tokens / elapsed:.1f}tok/s"
        )
        self._logger.debug(
            f"补全详情: 正文={_preview(result.content)} "
            f"思考段={len(result.reasoning or '')}字 模型={result.model}"
        )
        return result

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
    ) -> dict:
        """ 构造请求体；子类可覆盖注入方言参数（如 llama-server 的模板开关）。

        Args:
            messages: 内部消息列表
            max_new_tokens: 最大生成长度
            temperature: 采样温度
            stop: 停止序列

        Returns:
            dict: 可直接 POST 的请求体
        """
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
        """ 解析 /chat/completions 响应；子类可覆盖定制。

        Args:
            data: 响应 JSON

        Returns:
            CompletionResult
        """
        choice = data["choices"][0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        result = CompletionResult(
            content=message.get("content") or "",
            reasoning=message.get("reasoning_content"),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=Usage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            ),
            model=data.get("model") or (self._conf.model_name if self._conf else ""),
        )
        return result

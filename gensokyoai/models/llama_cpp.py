"""llama.cpp llama-server 提供者 —— 本地推理主力接入"""

import re

import msgspec

from ..core.registry import Registry
from ..schemas.model_schema import CompletionResult, Message, ToolCall
from .base import OpenAICompatProvider, split_think

_TEXT_TOOL_PATTERNS: tuple[re.Pattern, ...] = (
    # "调用 xxx" / "使用 xxx"
    re.compile(r"(?:调用|使用)\s+([a-zA-Z_][a-zA-Z0-9_]*)"),
    # "xxx(" —— 模型把工具名当函数写
    re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\("),
    # "需要 xxx 工具"
    re.compile(r"需要\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+工具"),
)
""" 文本喊话的工具名识别模式（本地小模型不走原生协议时的现实路径）"""

_ARGS_WINDOW = 120
""" 工具名之后查找 JSON 参数的字符窗口 """


def _json_args_after(text: str, position: int) -> str:
    """工具名位置之后的小窗口里找 JSON 对象作为参数；找不到/不合法返回 "{}"。

    花括号配对为朴素深度计数（参数内含花括号的极端场景会解析失败降级空参，
    好过把非 JSON 当参数传）。
    """
    window = text[position : position + _ARGS_WINDOW]
    start = window.find("{")
    if start == -1:
        return "{}"
    depth = 0
    for index in range(start, len(window)):
        char = window[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = window[start : index + 1]
                try:
                    msgspec.json.decode(candidate, type=dict)
                except msgspec.DecodeError:
                    return "{}"
                return candidate
    return "{}"


def extract_text_tool_calls(text: str) -> list[ToolCall]:
    """从自然语言里提取「文本喊话」的工具调用（去重保序，支持一次多个）。

    识别三类模式：`调用/使用 X`、`X(`、`需要 X 工具`；每个工具名之后的
    小窗口内找 JSON 对象作为参数（找不到 = 空参，调用方按无参工具执行）。
    真机实测：本地 Qwen 不走原生 tool_calls 协议，这是它的主要调用形态。

    Args:
        text: 模型的 thought / action_hint 文本

    Returns:
        list[ToolCall]: 提取到的调用（无则空列表）
    """
    found: list[tuple[int, str]] = []
    for pattern in _TEXT_TOOL_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), match.group(1)))
    found.sort(key=lambda item: item[0])

    calls: list[ToolCall] = []
    seen: set[str] = set()
    for position, name in found:
        if name in seen:
            continue
        seen.add(name)
        calls.append(
            ToolCall(
                id=f"call_{len(calls)}",
                name=name,
                arguments=_json_args_after(text, position),
            )
        )
    return calls


@Registry.register(name="llama_cpp", ext_type="model_provider")
class LlamaProvider(OpenAICompatProvider):
    """llama-server 专用提供者。

    差异点：
    - 消息序列化省略 None 字段（server 端严格校验，null 直接 500）
    - think=False 时注入 chat_template_kwargs 关掉模板思考
    - 实现 normalize_tool_calls 处理 Qwen 的自定义工具调用格式
    """

    log_tag = "LLAMA"

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
        stream: bool = False,
    ) -> dict:
        """请求体构造：按 think 配置注入模板思考开关。"""
        payload = super()._build_payload(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            stream=stream,
        )
        if self._conf and not self._conf.think:
            payload["chat_template_kwargs"] = {"thinking": False, "enable_thinking": False}
        return payload

    def _build_result(self, data: dict) -> CompletionResult:
        """llama-server 响应解析：思考段不落正文。"""
        result = super()._build_result(data)
        if result.reasoning is None and result.content:
            body, think = split_think(result.content)
            if think is not None:
                result = CompletionResult(
                    content=body,
                    reasoning=think,
                    finish_reason=result.finish_reason,
                    usage=result.usage,
                    model=result.model,
                    tool_calls=result.tool_calls,
                )
                self._logger.debug(f"已分离思考段 {len(think)} 字符")
        return result

    def normalize_tool_calls(
        self, result: CompletionResult, parsed_content: dict | None = None
    ) -> CompletionResult:
        """处理 Qwen 等模型的自定义工具调用格式。

        识别以下格式：
        1. OpenAI 标准格式：result.tool_calls（已有）
        2. Qwen 自定义协议：parsed_content["tool_calls"] = [{"name": "...", "args": {...}}]
        3. 文本喊话：parsed_content["thought"/"action_hint"] 中包含工具名

        Args:
            result: 原始补全结果
            parsed_content: 解析后的 JSON 内容

        Returns:
            标准化后的 CompletionResult（tool_calls 字段已填充）
        """
        # 1. 如果已有标准工具调用，直接返回
        if result.tool_calls:
            return result

        if not parsed_content:
            return result

        # 2. Qwen 自定义协议（JSON 中的 tool_calls 数组）
        raw_calls = parsed_content.get("tool_calls")
        if raw_calls and isinstance(raw_calls, list):
            tool_calls: list[ToolCall] = []
            for call in raw_calls:
                if isinstance(call, dict):
                    name = call.get("name", "")
                    args = call.get("args", "{}")
                    if isinstance(args, dict):
                        args_json = msgspec.json.encode(args).decode()
                    else:
                        args_json = str(args)
                    tool_calls.append(
                        ToolCall(
                            id=f"call_{len(tool_calls)}",
                            name=name,
                            arguments=args_json,
                        )
                    )

            if tool_calls:
                self._logger.info(f"检测到自定义协议工具调用: {[tc.name for tc in tool_calls]}")
                return CompletionResult(
                    content=result.content,
                    reasoning=result.reasoning,
                    finish_reason=result.finish_reason,
                    usage=result.usage,
                    model=result.model,
                    tool_calls=tool_calls,
                )

        # 3. 文本喊话（本地小模型不走原生协议时的现实路径）：
        #    识别「调用 工具名 {"参数": 值}」，支持一次多个，JSON 参数尽力提取
        thought_and_hint = (
            f"{parsed_content.get('thought', '')} {parsed_content.get('action_hint', '')}"
        )
        shouted = self._extract_text_tool_calls(thought_and_hint)
        if shouted:
            for call in shouted:
                if call.arguments == "{}":
                    self._logger.warning(
                        f"文本喊话工具调用: {call.name}（未识别到参数，按无参工具执行）"
                    )
                else:
                    self._logger.info(f"文本喊话工具调用: {call.name} {call.arguments}")
            return CompletionResult(
                content=result.content,
                reasoning=result.reasoning,
                finish_reason=result.finish_reason,
                usage=result.usage,
                model=result.model,
                tool_calls=shouted,
            )

        return result

    def _extract_text_tool_calls(self, text: str) -> list[ToolCall]:
        """从自然语言里提取「文本喊话」的工具调用（委托模块级函数，便于单测）。"""
        return extract_text_tool_calls(text)

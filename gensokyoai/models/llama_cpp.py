"""llama.cpp llama-server 提供者 —— 本地推理主力接入"""

import re

import msgspec

from ..core.registry import Registry
from ..schemas.model_schema import CompletionResult, Message, ToolCall
from .base import OpenAICompatProvider, split_think


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

        # 3. 文本喊话（从 thought/action_hint 中提取）
        thought_and_hint = (
            f"{parsed_content.get('thought', '')} {parsed_content.get('action_hint', '')}"
        )
        tool_names = self._extract_tool_names_from_text(thought_and_hint)
        if tool_names:
            # 只取第一个匹配的工具（简化处理）
            tool_name = tool_names[0]
            tool_calls = [
                ToolCall(
                    id="call_0",
                    name=tool_name,
                    arguments="{}",  # 默认空参数，具体参数由 BrainEngine 从上下文提取
                )
            ]
            self._logger.warning(
                f"检测到文本喊话工具调用: {tool_name}，但未生成标准格式，使用空参数"
            )
            return CompletionResult(
                content=result.content,
                reasoning=result.reasoning,
                finish_reason=result.finish_reason,
                usage=result.usage,
                model=result.model,
                tool_calls=tool_calls,
            )

        return result

    def _extract_tool_names_from_text(self, text: str) -> list[str]:
        """从文本中提取可能的工具名（启发式）"""
        tool_names = []

        # 匹配 "调用 xxx" 或 "使用 xxx" 模式
        patterns = [
            r"(?:调用|使用)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(",
            r"需要\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+工具",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, text)
            tool_names.extend(matches)

        return list(set(tool_names))  # 去重

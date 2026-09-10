"""AI模型相关数据"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import msgspec

type ToolFunc = Callable[..., Any] | Callable[..., Awaitable[Any]]


class ModelConfig(msgspec.Struct, frozen=True):
    """模型配置 —— **单一来源**。

    此前有 `schemas.ModelConfig` 与 `core.config.ModelSettings` 两个近乎重复的结构体，
    装配时把后者传给声明收前者的 `provider.config()`，靠鸭子类型蒙混。现已合并：

    - **装配 / 路由**：`provider` 决定用哪个 Provider 注册项；`context_window` 定会话预算
    - **Provider 调用**：地址 / 模型名 / think / streaming / 超时 / 工具限制

    顺带清掉两个**零消费者**的死字段：旧 `ModelSettings.reserve_for_output`
    与旧 `ModelConfig.extra`（输出预留已由每次调用的 `max_new_tokens` 承担）。
    """

    provider: str = "llama_cpp"
    """ Provider 注册名（对应 @Registry.register 的 name）"""
    base_url: str = "http://127.0.0.1:8080/v1"
    """ OpenAI 兼容服务地址（llama.cpp server / vLLM）"""
    token: str | None = None
    """ 模型访问 token """
    model_name: str = "qwen"
    """ 使用的模型名称 """
    think: bool = False
    """ 是否思考 """
    streaming: bool = True
    """ 是否允许流式投递 """
    invoker_type: Literal["openai", "openai-responses", "claude"] = "openai"
    """ 调用模式 """
    timeout: float = 120.0
    """ 请求超时时间（秒）"""
    context_window: int = 32768
    """ 模型上下文窗口 —— 该模型下会话的 token 预算（由装配层下发到 SessionManager）"""
    tool_timeout: float = 10.0
    """ 单次工具执行超时（秒）；与 core.toolkit 默认值保持一致 """
    tool_max_result_chars: int = 2000
    """ 工具结果最大字符数，超出截断（保护上下文窗口）"""


_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}
""" Python 标注 -> JSON Schema 类型映射 """


@dataclass(slots=True)
class ToolSpec:
    tool_func: ToolFunc
    params: dict[str, str] = field(default_factory=dict)
    desc: str = ""
    name: str = ""
    """ 工具对外名（注册名）；留空则用函数名 —— 统一注册名与函数名可能不一致的问题 """
    _is_coro: bool = False

    def __post_init__(self):
        # 1. 自动检测是否为协程
        self._is_coro = inspect.iscoroutinefunction(self.tool_func)

        # 2. 如果没给描述，就用函数的 docstring
        if not self.desc:
            self.desc = self.tool_func.__doc__ or "没有描述"

        # 3. 如果没给参数描述，尝试从签名里自动生成
        if not self.params:
            sig = inspect.signature(self.tool_func)
            for name, param in sig.parameters.items():
                # 跳过 *args 和 **kwargs
                if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue
                # 用类型注解作为默认描述，没有注解就写“未指定”
                anno = param.annotation
                desc_text = (
                    f"类型: {anno.__name__}" if anno != inspect.Parameter.empty else "未指定"
                )
                if param.default is not inspect.Parameter.empty:
                    desc_text += f" (默认: {param.default})"
                self.params[name] = desc_text

    def _tool_err(self, err: Exception) -> str:
        detail = str(err) if str(err) else "(这个异常没有具体内容)"
        return f"发生了错误：{type(err).__name__}: {detail}"

    @property
    def tool_name(self) -> str:
        """工具对外名：显式 name 优先，否则用函数名。"""
        return self.name or self.tool_func.__name__

    @property
    def is_async(self) -> bool:
        """是否为异步工具"""
        return self._is_coro

    def to_openai_tool(self) -> dict:
        """转成 OpenAI tools 声明格式（llama-server / vLLM 通用）。

        参数 JSON Schema 从函数签名推导：标注映射到 JSON 类型，
        无默认值的参数进入 required。

        Returns:
            dict: {"type": "function", "function": {...}}
        """
        sig = inspect.signature(self.tool_func)
        properties: dict[str, dict] = {}
        required: list[str] = []
        for name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            properties[name] = {
                "type": _JSON_TYPES.get(param.annotation, "string"),
                "description": self.params.get(name, ""),
            }
            if param.default is inspect.Parameter.empty:
                required.append(name)
        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.desc,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    def prompt(self) -> str:
        lines = [f"# 工具 {self.tool_name}", f"- 描述: {self.desc}", "## 参数"]

        if self.params:
            for name, desc in self.params.items():
                lines.append(f"- {name}: {desc}")
        else:
            lines.append("- 无参数")

        return "\n".join(lines)

    def invoke(self, **param) -> Any:
        try:
            # 如果是异步函数却在同步环境调用，直接报错提示
            if self._is_coro:
                raise RuntimeError("这是一个异步工具，请使用 await ainvoke() 调用")
            return self.tool_func(**param)
        except Exception as e:
            return self._tool_err(e)

    async def ainvoke(self, **param) -> Any:
        if not self._is_coro:
            raise RuntimeError("这不是异步工具，请使用 invoke() 直接调用 ")
        try:
            return await self.tool_func(**param)
        except Exception as e:
            return self._tool_err(e)


class ToolCall(msgspec.Struct, frozen=True):
    """模型请求的一次工具调用"""

    id: str = ""
    """ 调用 ID（回传 tool 消息时必须一致）"""
    name: str = ""
    """ 工具名 """
    arguments: str = "{}"
    """ 参数 JSON 字符串（OpenAI 协议约定为字符串而非对象）"""


class Message(msgspec.Struct, frozen=True):
    """一条对话消息"""

    role: Literal["system", "user", "assistant", "tool"]
    """ 角色 """
    content: str = ""
    """ 消息内容 """
    name: str | None = None
    """ 发送者名称（群聊场景区分参与者）"""
    tool_call_id: str | None = None
    """ role=tool 时对应的工具调用 ID """
    tool_calls: list[ToolCall] | None = None
    """ role=assistant 时模型请求的工具调用列表 """


class Usage(msgspec.Struct, frozen=True):
    """一次调用的 token 用量"""

    prompt_tokens: int = 0
    """ 输入消耗 """
    completion_tokens: int = 0
    """ 输出消耗 """


@dataclass(slots=True)
class StreamEvent:
    """流式补全的单个片段事件。

    `chat_stream()` 从模型侧逐块产出 StreamEvent：
    - 正文块：`delta` 为本块新增文本（`finish_reason`/`usage` 为 None）
    - 收尾块：`delta` 为空串，`finish_reason`（stop/length/...）与累计 `usage` 在此附上

    调用方按 `delta` 累积即可得到完整文本；`finish_reason="length"` 时触发半截续写。
    """

    delta: str
    """ 本块新增的文本片段 """
    finish_reason: str | None = None
    """ 收尾原因；仅末块给出 """
    usage: Usage | None = None
    """ 本次调用的累计 token 用量；仅末块给出（服务端透传时）"""


class CompletionResult(msgspec.Struct, frozen=True):
    """一次补全的结果"""

    content: str = ""
    """ 回复文本 """
    reasoning: str | None = None
    """ 思考内容（如有）"""
    finish_reason: str = "stop"
    """ 结束原因 """
    usage: Usage = Usage()
    """ token 用量 """
    model: str = ""
    """ 实际使用的模型 """
    tool_calls: list[ToolCall] | None = None
    """ 模型请求的工具调用（无则 None）"""


if __name__ == "__main__":

    def get_weather(city: str, unit: str = "celsius") -> str:
        """获取指定城市的天气"""
        return f"{city} 的天气很好"

    t = ToolSpec(get_weather)
    print(t.prompt())
    print("---")
    print(t.invoke(city="北京"))

""" AI模型相关数据 """

from typing import Literal, Callable, Any, Awaitable

import inspect
import msgspec
from dataclasses import dataclass, field

type ToolFunc = Callable[..., Any] | Callable[..., Awaitable[Any]]

class ModelConfig(msgspec.Struct, frozen=True):
    """ 模型配置 """
    base_url: str
    """ 模型请求URL """
    token: str | None = None
    """ 模型访问token """
    model_name: str = "gpt-4"
    """ 使用的模型名称 """
    think: bool = False
    """ 是否思考 """
    streaming: bool = True
    """ 是否流式传输 """
    invoker_type: Literal["openai", "openai-responses", "claude"] = "openai"
    """ 调用模式 """
    timeout: float = 30.0  # 新增：超时时间
    """ 请求超时时间(秒) """
    extra: dict = {}
    """ 额外参数 """

@dataclass(slots=True)
class ToolSpec:
    tool_func: ToolFunc
    params: dict[str, str] = field(default_factory=dict)
    desc: str = ""
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
                desc_text = f"类型: {anno.__name__}" if anno != inspect.Parameter.empty else "未指定"
                if param.default is not inspect.Parameter.empty:
                    desc_text += f" (默认: {param.default})"
                self.params[name] = desc_text

    def _tool_err(self, err: Exception) -> str:
        detail = str(err) if str(err) else "(这个异常没有具体内容)"
        return f"发生了错误：{type(err).__name__}: {detail}"
    
    def prompt(self) -> str:
        lines = [
            f"# 工具 {self.tool_func.__name__}",
            f"- 描述: {self.desc}",
            "## 参数"
        ]
        
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

class Message(msgspec.Struct, frozen=True):
    """ 一条对话消息 """
    role: Literal["system", "user", "assistant", "tool"]
    """ 角色 """
    content: str = ""
    """ 消息内容 """
    name: str | None = None
    """ 发送者名称（群聊场景区分参与者）"""
    tool_call_id: str | None = None
    """ role=tool 时对应的工具调用 ID """

class Usage(msgspec.Struct, frozen=True):
    """ 一次调用的 token 用量 """
    prompt_tokens: int = 0
    """ 输入消耗 """
    completion_tokens: int = 0
    """ 输出消耗 """

class CompletionResult(msgspec.Struct, frozen=True):
    """ 一次补全的结果 """
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

if __name__ == "__main__":
    def get_weather(city: str, unit: str = "celsius") -> str:
        """获取指定城市的天气"""
        return f"{city} 的天气很好"

    t = ToolSpec(get_weather)
    print(t.prompt())
    print("---")
    print(t.invoke(city="北京"))
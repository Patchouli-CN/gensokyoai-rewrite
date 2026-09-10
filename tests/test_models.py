"""模型层单元测试：消息序列化 / 思考段分离 / LlamaProvider 响应解析 / 工具调用"""

import msgspec

from gensokyoai.core.registry import Registry
from gensokyoai.models.base import (
    ModelProvider,
    OpenAICompatProvider,
    split_think,
    to_openai_messages,
)
from gensokyoai.models.llama_cpp import LlamaProvider
from gensokyoai.schemas.model_schema import (
    CompletionResult,
    Message,
    ModelConfig,
    ToolSpec,
    Usage,
)


def get_weather(city: str, unit: str = "celsius") -> str:
    """获取城市天气"""
    return f"{city} 晴, 25度"


async def fetch_quote(symbol: str) -> str:
    """异步查询行情"""
    return f"{symbol} 涨1%"


def test_to_openai_messages_omits_none_fields():
    """None 字段不序列化 —— llama-server 遇 "name": null 直接 500"""
    msgs = [Message(role="user", content="你好")]
    payload = msgspec.json.encode(to_openai_messages(msgs)).decode()
    assert payload == '[{"role":"user","content":"你好"}]'


def test_to_openai_messages_keeps_optional_fields():
    """显式给值的可选字段保留"""
    msgs = [Message(role="tool", content="结果", tool_call_id="call_1")]
    payload = to_openai_messages(msgs)[0]
    assert payload["tool_call_id"] == "call_1"


def test_split_think_separates_reasoning():
    """<think> 段剥离，正文保留"""
    body, think = split_think("<think>用户在打招呼</think>哟，是你啊。")
    assert body == "哟，是你啊。"
    assert think == "用户在打招呼"


def test_split_think_without_tags():
    """无思考段时原样返回"""
    body, think = split_think("直接回复")
    assert body == "直接回复"
    assert think is None


def test_llama_provider_build_result_splits_inline_think():
    """server 未分离思考段时，LlamaProvider 从正文剥离 <think>"""
    provider = LlamaProvider()
    result = provider._build_result(
        {
            "choices": [
                {"message": {"content": "<think>思考中</think>正文回复"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    assert result.content == "正文回复"
    assert result.reasoning == "思考中"


def test_llama_provider_prefers_reasoning_content():
    """server 已分离 reasoning_content 时直接采用，不再动正文"""
    provider = LlamaProvider()
    result = provider._build_result(
        {
            "choices": [{"message": {"content": "正文", "reasoning_content": "服务端思考"}}],
        }
    )
    assert result.content == "正文"
    assert result.reasoning == "服务端思考"


def test_registry_has_model_providers():
    """两个模型提供者均已注册"""
    assert Registry.get("llama_cpp") is LlamaProvider
    from gensokyoai.models.qwen_local import QwenLocalProvider

    assert Registry.get("qwen_local") is QwenLocalProvider


def _llama_payload(think: bool) -> dict:
    """构造带 think 配置的 LlamaProvider 请求体"""
    from gensokyoai.schemas.model_schema import ModelConfig

    provider = LlamaProvider().config(
        ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen", think=think),
    )
    return provider._build_payload(
        [Message(role="user", content="你好")],
        max_new_tokens=400,
        temperature=0.4,
        stop=None,
    )


def test_llama_payload_disables_thinking_by_default():
    """think=False 时注入 chat_template_kwargs 关模板思考（Qwen3 系默认 thinking=1）"""
    payload = _llama_payload(think=False)
    assert payload["chat_template_kwargs"]["thinking"] is False
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_llama_payload_keeps_thinking_when_enabled():
    """think=True 时不注入开关，走模板默认"""
    payload = _llama_payload(think=True)
    assert "chat_template_kwargs" not in payload


def test_to_openai_messages_serializes_tool_calls():
    """assistant 消息的 tool_calls 按 OpenAI 协议序列化"""
    from gensokyoai.schemas.model_schema import ToolCall

    msgs = [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="call_1", name="get_weather", arguments='{"city": "北京"}'),
            ],
        )
    ]
    payload = to_openai_messages(msgs)[0]
    assert payload["tool_calls"][0] == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "北京"}'},
    }


def test_tool_spec_openai_schema():
    """工具声明从签名推导 JSON Schema：类型映射 + required"""
    tool = ToolSpec(get_weather, params={"city": "城市名"})
    schema = tool.to_openai_tool()
    func = schema["function"]
    assert func["name"] == "get_weather"
    assert func["description"] == "获取城市天气"
    props = func["parameters"]["properties"]
    assert props["city"] == {"type": "string", "description": "城市名"}
    assert props["unit"]["type"] == "string"
    assert func["parameters"]["required"] == ["city"]


def test_build_result_parses_tool_calls():
    """响应中的 tool_calls 解析为 ToolCall 列表"""
    provider = LlamaProvider()
    result = provider._build_result(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "上海"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    )
    assert result.tool_calls is not None
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].arguments == '{"city": "上海"}'


class _StubProvider(LlamaProvider):
    """不发真实 HTTP，按序返回预设响应并记录请求体"""

    def __init__(self, responses: list[dict]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.payloads: list[dict] = []

    async def _post_json(self, http, url, payload, headers, started) -> dict:
        self.payloads.append(payload)
        return self._responses.pop(0)


def _tool_call_response(cid: str, name: str, args: str) -> dict:
    """构造一轮带 tool_calls 的响应"""
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": cid,
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


def _final_response(text: str) -> dict:
    """构造一轮普通文本响应"""
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 20},
    }


def _configured(responses: list[dict]) -> _StubProvider:
    """带配置的 StubProvider"""
    return _StubProvider(responses).config(
        ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen"),
    )


async def test_tool_loop_executes_sync_and_unknown_tools():
    """同步工具被执行、未知工具回传错误文本、最终拿到收尾回复与累计用量"""
    provider = _configured(
        [
            _tool_call_response("call_1", "get_weather", '{"city": "北京"}'),
            _tool_call_response("call_2", "no_such_tool", "{}"),
            _final_response("北京今天晴"),
        ]
    )
    result = await provider.chat(
        [Message(role="user", content="北京天气？")],
        tools=[ToolSpec(get_weather)],
    )
    assert result.content == "北京今天晴"
    assert result.usage.prompt_tokens == 50 + 50 + 80
    assert result.usage.completion_tokens == 10 + 10 + 20

    # 第一轮请求带工具声明
    first = provider.payloads[0]
    assert first["tools"][0]["function"]["name"] == "get_weather"
    assert first["tool_choice"] == "auto"

    # 第二轮请求追加 assistant(tool_calls) + 同步工具结果
    second_msgs = provider.payloads[1]["messages"]
    assert [m["role"] for m in second_msgs] == ["user", "assistant", "tool"]
    assert "北京 晴" in second_msgs[-1]["content"]
    assert second_msgs[-1]["tool_call_id"] == "call_1"

    # 第三轮请求追加未知工具的 assistant + 错误文本回传
    third_msgs = provider.payloads[2]["messages"]
    assert [m["role"] for m in third_msgs] == ["user", "assistant", "tool", "assistant", "tool"]
    assert any("未知工具" in m["content"] for m in third_msgs if m["role"] == "tool")


async def test_tool_loop_executes_async_tool():
    """异步工具走 ainvoke 路径"""
    provider = _configured(
        [
            _tool_call_response("call_a", "fetch_quote", '{"symbol": "AAPL"}'),
            _final_response("AAPL 涨了"),
        ]
    )
    result = await provider.chat(
        [Message(role="user", content="AAPL 行情？")],
        tools=[ToolSpec(fetch_quote)],
    )
    assert result.content == "AAPL 涨了"
    tool_msgs = [m for m in provider.payloads[1]["messages"] if m["role"] == "tool"]
    assert any("AAPL 涨1%" in m["content"] for m in tool_msgs)


async def test_tool_loop_bad_arguments_json_degrades():
    """工具参数 JSON 损坏时按空参数执行，不崩链"""
    provider = _configured(
        [
            _tool_call_response("call_x", "get_weather", "不是JSON"),
            _final_response("默认城市晴"),
        ]
    )
    result = await provider.chat(
        [Message(role="user", content="天气？")],
        tools=[ToolSpec(get_weather)],
    )
    assert result.content == "默认城市晴"
    tool_msgs = [m for m in provider.payloads[1]["messages"] if m["role"] == "tool"]
    # city 缺参由 ToolSpec.invoke 的异常兜底转成错误文本，而非中断
    assert len(tool_msgs) == 1


class _StreamingStub(OpenAICompatProvider):
    """不发真实 HTTP，按脚本产出 SSE 事件并记录请求体"""

    def __init__(self, events: list[dict]) -> None:
        super().__init__()
        self._events = list(events)
        self.payloads: list[dict] = []

    async def _iter_sse_events(self, http, url, payload, headers):
        self.payloads.append(payload)
        for ev in self._events:
            yield ev


def test_supports_streaming_reflects_config():
    """supports_streaming 由配置 streaming 决定；未配置/缺省为 False"""
    on = LlamaProvider().config(
        ModelConfig(base_url="http://x/v1", model_name="qwen", streaming=True)
    )
    assert on.supports_streaming is True
    off = LlamaProvider().config(
        ModelConfig(base_url="http://x/v1", model_name="qwen", streaming=False)
    )
    assert off.supports_streaming is False
    assert LlamaProvider().supports_streaming is False


def test_default_payload_stream_flag_off():
    """chat() 缓冲路径恒 stream=False，不受配置 streaming 影响"""
    payload = _llama_payload(think=False)
    assert payload["stream"] is False


async def test_chat_stream_parses_sse():
    """真流式：逐块解析 SSE delta，末块附 finish_reason 与累计 usage"""
    events = [
        {"choices": [{"delta": {"content": "唔"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "……让我想想"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8},
        },
    ]
    provider = _StreamingStub(events).config(
        ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen"),
    )
    chunks = [ev async for ev in provider.chat_stream([Message(role="user", content="hi")])]

    assert [c.delta for c in chunks[:-1]] == ["唔", "……让我想想"]
    assert chunks[-1].delta == ""
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 10
    assert chunks[-1].usage.completion_tokens == 8
    # 流式请求体应带 stream=True
    assert provider.payloads[0]["stream"] is True


async def test_chat_stream_llama_payload_keeps_think_switch():
    """LlamaProvider 流式请求体：stream=True 同时保留 think=False 的模板思考开关"""

    class _LlamaStreamStub(LlamaProvider):
        def __init__(self):
            super().__init__()
            self.payloads = []

        async def _iter_sse_events(self, http, url, payload, headers):
            self.payloads.append(payload)
            yield {"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}

    stub = _LlamaStreamStub().config(
        ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen", think=False),
    )
    _ = [ev async for ev in stub.chat_stream([Message(role="user", content="hi")])]
    payload = stub.payloads[0]
    assert payload["stream"] is True
    assert payload["chat_template_kwargs"]["thinking"] is False


async def test_chat_stream_base_fallback_single_event():
    """未实现真流式的子类，兜底 chat() 整段一次性产出"""

    class _PlainProvider(ModelProvider):
        def config(self, conf):
            self._conf = conf
            return self

        async def chat(
            self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None
        ):
            return CompletionResult(
                content="整段文本",
                finish_reason="stop",
                usage=Usage(prompt_tokens=5, completion_tokens=3),
            )

    p = _PlainProvider()
    chunks = [ev async for ev in p.chat_stream([Message(role="user", content="hi")])]
    assert len(chunks) == 1
    assert chunks[0].delta == "整段文本"
    assert chunks[0].finish_reason == "stop"
    assert chunks[0].usage.completion_tokens == 3


async def test_chat_stream_requests_usage_explicitly():
    """流式请求必须显式索取 usage

    实测 llama-server：不带 `stream_options` 则**完全没有 usage 事件**，
    导致 token 计量恒为 0（健康监控与配额都统计不到 responder 用量）。
    """
    provider = _StreamingStub(
        [{"choices": [{"delta": {"content": ""}, "finish_reason": "stop"}]}]
    ).config(ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen"))

    _ = [ev async for ev in provider.chat_stream([Message(role="user", content="hi")])]
    assert provider.payloads[0]["stream_options"] == {"include_usage": True}


async def test_chat_stream_parses_usage_and_cached_tokens():
    """末块带 usage 时能解析 token 与前缀缓存命中量"""
    events = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        },
    ]
    provider = _StreamingStub(events).config(
        ModelConfig(base_url="http://127.0.0.1:8080/v1", model_name="qwen")
    )
    chunks = [ev async for ev in provider.chat_stream([Message(role="user", content="hi")])]

    terminal = chunks[-1]
    assert terminal.usage is not None
    assert terminal.usage.prompt_tokens == 100
    assert terminal.usage.completion_tokens == 5
    assert terminal.usage.cached_tokens == 80, "前缀缓存命中量"


def test_build_result_parses_cached_tokens():
    """非流式响应同样解析前缀缓存命中量"""
    provider = LlamaProvider()
    result = provider._build_result(
        {
            "choices": [{"message": {"content": "正文"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 50,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        }
    )
    assert result.usage.prompt_tokens == 50
    assert result.usage.cached_tokens == 40

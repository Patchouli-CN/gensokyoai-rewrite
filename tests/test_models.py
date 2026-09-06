"""模型层单元测试：消息序列化 / 思考段分离 / LlamaProvider 响应解析"""

import msgspec

from gensokyoai.core.registry import Registry
from gensokyoai.models.base import split_think, to_openai_messages
from gensokyoai.models.llama_cpp import LlamaProvider
from gensokyoai.schemas.model_schema import Message


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
    result = provider._build_result({
        "choices": [{"message": {"content": "<think>思考中</think>正文回复"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    assert result.content == "正文回复"
    assert result.reasoning == "思考中"


def test_llama_provider_prefers_reasoning_content():
    """server 已分离 reasoning_content 时直接采用，不再动正文"""
    provider = LlamaProvider()
    result = provider._build_result({
        "choices": [{"message": {"content": "正文", "reasoning_content": "服务端思考"}}],
    })
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
        [Message(role="user", content="你好")], max_new_tokens=400, temperature=0.4, stop=None,
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

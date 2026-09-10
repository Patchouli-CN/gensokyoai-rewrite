"""Qwen 通用 OpenAI 兼容端点提供者（云端兼容网关 / vLLM 等）"""

from ..core.registry import Registry
from .base import OpenAICompatProvider


@Registry.register(name="qwen_local", ext_type="model_provider")
class QwenLocalProvider(OpenAICompatProvider):
    """通用 OpenAI 兼容提供者：无本地 server 特殊处理，走基类默认行为"""

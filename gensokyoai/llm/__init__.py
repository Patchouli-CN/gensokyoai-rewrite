"""模型适配层 —— 一家厂商一个适配器，引擎只认 `Provider.complete`。"""

from .anthropic import AnthropicProvider
from .base import Provider
from .billing import BUILTIN, Prices, Quota, UsageLedger, price_for
from .caps import ALL as ALL_CAPABILITIES
from .caps import AUDIO, IMAGE, TEXT, VIDEO, detect, normalize
from .openai import OpenAIProvider
from .types import Completion, Image, Part, Text, ToolCall, Turn, Usage

PROVIDERS = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}
""" 可选的厂商名 """

__all__ = [
    "ALL_CAPABILITIES",
    "AUDIO",
    "BUILTIN",
    "IMAGE",
    "PROVIDERS",
    "TEXT",
    "VIDEO",
    "AnthropicProvider",
    "Completion",
    "Image",
    "OpenAIProvider",
    "Part",
    "Prices",
    "Provider",
    "Quota",
    "Text",
    "ToolCall",
    "Turn",
    "Usage",
    "UsageLedger",
    "build_provider",
    "detect",
    "normalize",
    "price_for",
]


def build_provider(kind: str, base_url: str, **kwargs) -> Provider:
    """按厂商名造一个适配器。

    Args:
        kind: 厂商名（见 PROVIDERS）
        base_url: 端点地址
        **kwargs: 传给适配器（model / api_key / headers / think / extra / timeout）

    Returns:
        Provider: 适配器实例

    Raises:
        ValueError: 不认识的厂商名
    """
    factory = PROVIDERS.get(kind.lower())
    if factory is None:
        raise ValueError(f"不认识的厂商 {kind!r}，可选: {sorted(PROVIDERS)}")
    return factory(base_url, **kwargs)

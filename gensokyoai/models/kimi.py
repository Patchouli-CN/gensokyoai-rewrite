"""Kimi（Moonshot）云端提供者 —— OpenAI 兼容端点 + k2.6 采样方言。

k2.6 起 Moonshot 把采样参数锁成模式绑定（传别的值直接 400 invalid temperature）：

- **思考模式**（默认）：只允许 `temperature=1`，reasoning token 按输出价计费
- **非思考模式**（`reasoning_effort="none"`）：只允许 `temperature=0.6`

本框架的理念是「用外部工程把推理挤出上下文」（思考链 / 裁判都是框架层的活），
模型的内置推理既重复又烧钱（输出价），故 `think=False`（默认）映射为非思考
模式 + 0.6；`think=True` 时才走模型自己的推理（温度锁 1）。

框架各模块的温度旋钮（裁判 0.2 等）在这两个锁值下都无意义，统一在出口钳制。
"""

from ..core.registry import Registry
from ..schemas.model_schema import Message
from .base import OpenAICompatProvider

_TEMP_THINKING = 1.0
""" 思考模式锁定的温度 """

_TEMP_NON_THINKING = 0.6
""" 非思考模式锁定的温度 """


@Registry.register(name="kimi", ext_type="model_provider")
class KimiProvider(OpenAICompatProvider):
    """Kimi（Moonshot）：按 think 配置映射 k2.6 的推理模式与温度锁值。"""

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
        stream: bool = False,
    ) -> dict:
        payload = super()._build_payload(
            messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stop=stop,
            stream=stream,
        )
        if self._conf is not None and self._conf.think:
            payload["temperature"] = _TEMP_THINKING
        else:
            payload["reasoning_effort"] = "none"
            payload["temperature"] = _TEMP_NON_THINKING
        return payload

""" llama.cpp llama-server 提供者 —— 本地推理主力接入 """

from ..core.registry import Registry
from ..schemas.model_schema import CompletionResult, Message
from .base import OpenAICompatProvider, split_think

@Registry.register(name="llama_cpp", ext_type="model_provider")
class LlamaProvider(OpenAICompatProvider):
    """ llama-server 专用提供者。

    差异点（其余走 OpenAI 兼容基类）：
    - 消息序列化省略 None 字段（server 端严格校验，null 直接 500）
    - think=False 时注入 chat_template_kwargs 关掉模板思考（Qwen3 系模板默认
      thinking=1，会先吃掉全部生成预算再出正文，结构化任务因此拿不到输出）
    - thinking 意外开启时优先读 reasoning_content，缺失则从正文剥离 <think> 段
    """

    log_tag = "LLAMA"

    def _build_payload(
        self,
        messages: list[Message],
        *,
        max_new_tokens: int,
        temperature: float,
        stop: list[str] | None,
    ) -> dict:
        """ 请求体构造：按 think 配置注入模板思考开关。

        Args:
            messages: 内部消息列表
            max_new_tokens: 最大生成长度
            temperature: 采样温度
            stop: 停止序列

        Returns:
            dict: 含 chat_template_kwargs 的请求体（需要时）
        """
        payload = super()._build_payload(
            messages, max_new_tokens=max_new_tokens, temperature=temperature, stop=stop,
        )
        if self._conf and not self._conf.think:
            # thinking / enable_thinking 覆盖 Qwen3 与 Qwen3.5+ 两代模板变量名，
            # 模板未用的变量会被 jinja 静默忽略
            payload["chat_template_kwargs"] = {"thinking": False, "enable_thinking": False}
        return payload

    def _build_result(self, data: dict) -> CompletionResult:
        """ llama-server 响应解析：思考段不落正文。

        Args:
            data: /chat/completions 响应 JSON

        Returns:
            CompletionResult: reasoning 含思考内容，content 只保留正文
        """
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
                )
                self._logger.debug(f"已分离思考段 {len(think)} 字符")
        return result

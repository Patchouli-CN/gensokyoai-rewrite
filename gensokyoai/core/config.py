"""配置加载 —— msgspec 结构化 + YAML。支持多模型路由配置。"""

from pathlib import Path
from typing import Literal

import msgspec
import yaml


class ModelSettings(msgspec.Struct, frozen=True):
    """单个模型接入配置"""

    provider: str = "llama_cpp"
    """ 模型提供者注册名（对应 @Registry.register 的 name）"""
    base_url: str = "http://127.0.0.1:8080/v1"
    """ OpenAI 兼容服务地址（llama.cpp server / vLLM）"""
    token: str | None = None
    """ 访问 token """
    model_name: str = "qwen"
    """ 模型名称 """
    think: bool = False
    """ 是否思考 """
    streaming: bool = True
    """ 是否流式 """
    invoker_type: Literal["openai", "openai-responses", "claude"] = "openai"
    """ 调用模式 """
    timeout: float = 120.0
    """ 请求超时(秒) """
    context_window: int = 8192
    """ 模型上下文窗口 """
    reserve_for_output: int = 2048
    """ 给生成预留的 token 数 """
    tool_timeout: float = 10.0
    """ 单次工具执行超时（秒）"""
    tool_max_result_chars: int = 2000
    """ 工具结果最大字符数，超出截断（保护上下文窗口）"""


class ResourceSettings(msgspec.Struct, frozen=True):
    """资源闸门 / 限流配置（保护单模型稀缺资源）"""

    enabled: bool = True
    """ 是否启用资源闸门 """
    max_concurrent: int = 1
    """ 全局并发上限（本地单模型建议 1，避免并发打爆显存）"""
    rpm: int = 0
    """ 每租户每分钟模型调用上限；0 不限 """
    concurrency: int = 1
    """ 每租户并发上限 """
    calls_per_day: int = 0
    """ 每租户每日模型调用上限；0 不限 """
    tokens_per_day: int = 0
    """ 每租户每日 token 预算；0 不限 """
    ingress_rate: float = 0.0
    """ 入口令牌桶速率（条/秒）；0 表示关闭入口限流 """
    ingress_burst: int = 0
    """ 入口令牌桶容量（允许的突发条数）"""


class GensokyoConfig(msgspec.Struct, frozen=True):
    """顶层配置"""

    default_model: ModelSettings = ModelSettings()
    """ 默认模型（未指定模块时使用）"""

    brain: ModelSettings = ModelSettings()
    """ Brain 决策模块使用的模型 """

    responder: ModelSettings = ModelSettings()
    """ Responder 表达模块使用的模型 """

    ooc: ModelSettings | None = None
    """ OOC 审计使用的模型（可选，默认用 brain）"""

    memorizer: ModelSettings | None = None
    """ 记忆压缩使用的模型（可选，默认用 brain）"""

    resource: ResourceSettings = ResourceSettings()
    """ 资源闸门 / 限流配置 """


def load_config(path: str | Path) -> GensokyoConfig:
    """读取 YAML 配置并校验为强类型配置对象。

    Args:
        path: 配置文件路径（YAML）

    Returns:
        GensokyoConfig: 缺省字段自动补全为 Struct 默认值

    Raises:
        FileNotFoundError: 配置文件不存在
        msgspec.ValidationError: 配置字段类型不符
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return msgspec.convert(data, GensokyoConfig, strict=False)

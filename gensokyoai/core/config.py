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

"""配置加载 —— msgspec 结构化 + YAML。支持多模型路由配置。

模型配置**复用 `schemas.model_schema.ModelConfig`**（单一来源），
本模块只负责把它组合进顶层配置并解析 YAML。
"""

from pathlib import Path

import msgspec
import yaml

from ..schemas.model_schema import ModelConfig

__all__ = ["GensokyoConfig", "ModelConfig", "ResourceSettings", "load_config"]


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

    default_model: ModelConfig = ModelConfig()
    """ 默认模型（未指定模块时使用）"""

    brain: ModelConfig = ModelConfig()
    """ Brain 决策模块使用的模型 """

    responder: ModelConfig = ModelConfig()
    """ Responder 表达模块使用的模型 """

    ooc: ModelConfig | None = None
    """ OOC 审计使用的模型（可选，默认用 brain）"""

    memorizer: ModelConfig | None = None
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

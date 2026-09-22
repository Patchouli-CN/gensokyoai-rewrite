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


class GateSettings(msgspec.Struct, frozen=True):
    """发言门控配置（jev 式混合门控，见 core/brain/gate.py）

    门控是行为变更：代码默认 `enabled=False`（直接构造世界时维持「每条都回」的
    旧行为）；`config/settings.yaml` 显式打开。
    """

    enabled: bool = False
    """ 是否启用反应路径门控（私聊/被 @ 由规则直判，不受影响） """
    judge: str = "local"
    """ 裁判后端：local=主模型当裁判 | typesafe=真 jev（需装 .[jev]）| none=纯规则 """
    typesafe_api_key: str = ""
    """ TypeSafe API key（judge=typesafe；空则用 SDK 环境变量） """
    typesafe_model: str = "jev-latest"
    """ jev 模型名 """
    group_threshold: float = 0.6
    """ 群聊未点名时，should_reply 达到该值才接话（调低更活跃） """
    search_threshold: float = 0.1
    """ needs_search 超过该值视为「可能要查证」（当前仅落日志） """
    timeout_ms: int = 60000
    """ 单次裁判调用超时（local 走 wait_for；typesafe 传给 SDK）。
        默认 60s：本地模型单次调用 10~25s，4s 量级会让裁判永远超时降级 """
    route_by_model: bool = True
    """ 档位路由模型化：有裁判时，连「私聊 / 被 @」也问一次裁判，用 needs_deep
        分数决定推理档位（弥补规则路由看不懂情绪/关系的短板）；
        False 或裁判不可用时回落规则 route() """
    deep_cuts: tuple[float, float, float] = (0.3, 0.6, 0.85)
    """ needs_deep 分数 -> 档位的三个切点（进 MID / HIGH / MAX 的线） """
    max_new_tokens: int = 128
    """ local 裁判的输出预算（三个概率 + 题名，128 足够） """
    temperature: float = 0.2
    """ local 裁判的采样温度（低温求稳） """
    presence_window_s: float = 300.0
    """ 活跃度统计窗口秒数（对齐 qqbot 的 5 分钟） """


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

    gate: GateSettings = GateSettings()
    """ 发言门控配置 """

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

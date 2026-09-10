"""SessionManager 装配工厂 —— 根据配置创建并装配多模型路由"""

from ..utils.logger import LoggerManager
from .config import GensokyoConfig, ModelSettings
from .registry import Registry
from .resource import GatedBackend, ResourceGate
from .session_manager import SessionManager

_logger = LoggerManager.get_logger("FACTORY")


def build_resource_gate(config: GensokyoConfig) -> ResourceGate | None:
    """按配置构建资源闸门；未启用时返回 None。

    Args:
        config: GensokyoConfig 配置对象

    Returns:
        ResourceGate | None: 启用则返回闸门实例，否则 None
    """
    res = config.resource
    if not res.enabled:
        _logger.info("资源闸门未启用")
        return None
    gate = ResourceGate(
        max_concurrent=res.max_concurrent,
        default_rpm=res.rpm,
        default_concurrency=res.concurrency,
        default_calls_per_day=res.calls_per_day,
        default_tokens_per_day=res.tokens_per_day,
    )
    _logger.info(
        f"资源闸门已启用: 全局并发={res.max_concurrent} 每租户 rpm={res.rpm} "
        f"每日调用={res.calls_per_day} 每日 token={res.tokens_per_day}"
    )
    return gate


def build_session_manager(
    config: GensokyoConfig, gate: ResourceGate | None = None
) -> SessionManager:
    """根据 GensokyoConfig 构建装配好的 SessionManager。

    规则：
    - 默认模型：config.default_model
    - brain.think → config.brain
    - responder → config.responder
    - brain.ooc → config.ooc 或 config.brain（默认）
    - memorizer.compress → config.memorizer 或 config.brain（默认）

    Args:
        config: GensokyoConfig 配置对象
        gate: 可选的资源闸门；传入则每个 backend 都被包一层配额/并发控制

    Returns:
        已经装配好多个 backend 的 SessionManager
    """
    sessions = SessionManager()

    # 1. 创建默认 backend
    default_provider = Registry.get(config.default_model.provider)
    default_backend = _maybe_gate(default_provider().config(config.default_model), gate)
    sessions.set_default_backend(default_backend)
    _logger.info(f"默认模型: {config.default_model.model_name} ({config.default_model.provider})")

    # 2. 注册 Brain 模型
    brain_backend = _create_backend(config.brain, gate)
    sessions.register_backend("brain.think", brain_backend)
    _logger.info(f"Brain 模型: {config.brain.model_name} ({config.brain.provider})")

    # 3. 注册 Responder 模型
    responder_backend = _create_backend(config.responder, gate)
    sessions.register_backend("responder", responder_backend)
    _logger.info(f"Responder 模型: {config.responder.model_name} ({config.responder.provider})")

    # 4. 注册 OOC 模型（可选，默认用 brain）
    ooc_settings = config.ooc or config.brain
    ooc_backend = _create_backend(ooc_settings, gate)
    sessions.register_backend("brain.ooc", ooc_backend)
    _logger.info(f"OOC 模型: {ooc_settings.model_name} ({ooc_settings.provider})")

    # 5. 注册记忆压缩模型（可选，默认用 brain）
    mem_settings = config.memorizer or config.brain
    mem_backend = _create_backend(mem_settings, gate)
    sessions.register_backend("memorizer.compress", mem_backend)
    _logger.info(f"Memorizer 模型: {mem_settings.model_name} ({mem_settings.provider})")

    return sessions


def _create_backend(settings: ModelSettings, gate: ResourceGate | None = None):
    """根据 ModelSettings 创建 backend 实例（可选包一层资源闸门）"""
    provider_cls = Registry.get(settings.provider)
    backend = provider_cls().config(settings)
    return _maybe_gate(backend, gate)


def _maybe_gate(backend, gate: ResourceGate | None):
    """把 backend 包进资源闸门；gate 为空则原样返回。"""
    if gate is None:
        return backend
    return GatedBackend(backend, gate)

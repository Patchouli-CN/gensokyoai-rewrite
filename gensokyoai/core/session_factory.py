""" SessionManager 装配工厂 —— 根据配置创建并装配多模型路由 """

from .registry import Registry
from .config import GensokyoConfig, ModelSettings
from .session_manager import SessionManager
from ..utils.logger import LoggerManager

_logger = LoggerManager.get_logger("FACTORY")

def build_session_manager(config: GensokyoConfig) -> SessionManager:
    """ 根据 GensokyoConfig 构建装配好的 SessionManager。
    
    规则：
    - 默认模型：config.default_model
    - brain.think → config.brain
    - responder → config.responder
    - brain.ooc → config.ooc 或 config.brain（默认）
    - memorizer.compress → config.memorizer 或 config.brain（默认）
    
    Args:
        config: GensokyoConfig 配置对象
        
    Returns:
        已经装配好多个 backend 的 SessionManager
    """
    sessions = SessionManager()
    
    # 1. 创建默认 backend
    default_provider = Registry.get(config.default_model.provider)
    default_backend = default_provider().config(config.default_model)
    sessions.set_default_backend(default_backend)
    _logger.info(f"默认模型: {config.default_model.model_name} ({config.default_model.provider})")
    
    # 2. 注册 Brain 模型
    brain_backend = _create_backend(config.brain)
    sessions.register_backend("brain.think", brain_backend)
    _logger.info(f"Brain 模型: {config.brain.model_name} ({config.brain.provider})")
    
    # 3. 注册 Responder 模型
    responder_backend = _create_backend(config.responder)
    sessions.register_backend("responder", responder_backend)
    _logger.info(f"Responder 模型: {config.responder.model_name} ({config.responder.provider})")
    
    # 4. 注册 OOC 模型（可选，默认用 brain）
    ooc_settings = config.ooc or config.brain
    ooc_backend = _create_backend(ooc_settings)
    sessions.register_backend("brain.ooc", ooc_backend)
    _logger.info(f"OOC 模型: {ooc_settings.model_name} ({ooc_settings.provider})")
    
    # 5. 注册记忆压缩模型（可选，默认用 brain）
    mem_settings = config.memorizer or config.brain
    mem_backend = _create_backend(mem_settings)
    sessions.register_backend("memorizer.compress", mem_backend)
    _logger.info(f"Memorizer 模型: {mem_settings.model_name} ({mem_settings.provider})")
    
    return sessions

def _create_backend(settings: ModelSettings):
    """ 根据 ModelSettings 创建 backend 实例 """
    provider_cls = Registry.get(settings.provider)
    return provider_cls().config(settings)

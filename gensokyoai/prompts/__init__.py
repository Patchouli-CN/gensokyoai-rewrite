"""提示词模块，专门放提示词。模板经 PromptManager 装饰器注册，提示词不散落在业务代码里。"""

from .cache import CacheStats, PromptCache
from .manager import PromptManager, prompt_mgr

__all__ = [
    "CacheStats",
    "PromptCache",
    "PromptManager",
    "prompt_mgr",
]

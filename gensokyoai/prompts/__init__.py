""" 提示词模块，专门放提示词。模板文件 + PromptManager 管理，提示词不散落在业务代码里。 """

from .manager import PromptManager, get_prompt, prompt_raw

__all__ = [
    "PromptManager",
    "get_prompt",
    "prompt_raw",
]

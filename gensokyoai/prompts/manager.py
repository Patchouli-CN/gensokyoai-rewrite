""" 提示词集中管理 —— 模板文件 + 占位符渲染。

模板为 prompts/ 下的 .txt 文件，文件名（去扩展名）即模板名。
占位符用 {{var}} 双花括号语法，模板里的 JSON 示例等单层花括号按字面处理。
"""

from ..schemas.prompt_schema import Prompt
from typing import Callable

class PromptManager:
    """ 
    提示词模板管理器：按名加载模板并渲染。
    支持装饰器注册，内置简单缓存机制。
    """
    
    _registered: dict[str, Prompt] = {}
    
    def __init__(self, enable_cache: bool = True) -> None:
        self._cache: dict[str, str] = {}
        self._enable_cache = enable_cache
        
    @classmethod
    def prompt(cls, name: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
        """ 
        注册装饰器
        """
        def decorator(prompt_func: Callable[[], str]) -> Callable[[], str]:
            # 立即执行函数获取模板字符串
            template_str = prompt_func()
            
            if name in cls._registered:
                raise RuntimeError(f"提示词模板重复注册: {name}")
                
            cls._registered[name] = Prompt(name=name, template=template_str)
            return prompt_func # 返回原函数，保持函数属性不变
        return decorator
    
    def render(self, name: str, **params) -> str:
        """ 
        返回渲染后的文本。
        - 如果没有参数且开启缓存，直接返回缓存结果。
        - 如果有参数，每次重新渲染（因为内容随参数变化）。
        """
        # 1. 尝试从缓存获取（仅针对无参数的静态模板）
        if self._enable_cache and not params and name in self._cache:
            return self._cache[name]
            
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
            
        # 2. 执行渲染
        result = self._registered[name].render(**params)
        
        # 3. 存入缓存（仅当没有动态参数时）
        if self._enable_cache and not params:
            self._cache[name] = result
            
        return result
    
    def raw(self, name: str) -> Prompt:
        """ 返回原始 Prompt 对象，用于调试或查看元数据 """
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
        return self._registered[name]
        

# 初始化单例
prompt_mgr = PromptManager()

@prompt_mgr.prompt("brain.think.user")
def brain_think_user() -> str:
    return """
[人设]
$persona

[场景] $sender: $content

[最近上下文]
$context

[相关记忆]
$memory
"""

@prompt_mgr.prompt("brain.think")
def brain_think() -> str:
    return \
"""你是角色扮演引擎的决策模块。基于人设、场景与记忆，判断用户意图与情绪，并产出一条贴合人设的初稿回复。只输出一个 JSON 对象，不要输出任何其他内容：
{"intent": "意图", "emotion": "情绪", "draft": "初稿回复", "confidence": 0.0}"""

@prompt_mgr.prompt("memory.compress")
def mem_compress() -> str:
    return "你是记忆压缩器。把一批对话记忆压缩成一段简短概要，只保留对角色有长期价值的关键信息（人名、承诺、事实、情感转折），丢弃寒暄与重复内容。直接输出概要文本。"

@prompt_mgr.prompt("ooc.audit.user")
def ooc_audit_user() -> str:
    return """
[人设]
$persona

[回复]
$draft
"""

@prompt_mgr.prompt("ooc.audit")
def ooc_audit() -> str: 
    return """你是角色扮演的 OOC 审计员。判断给定回复是否偏离角色人设（是否出戏、是否暴露 AI 身份、是否符合角色性格）。只输出一个 JSON 对象：
{"is_ooc": false, "confidence": 0.0, "reason": "理由"}
"""

@prompt_mgr.prompt("responder.user")
def responder() -> str:
    return """
$sender说: $content
[决策提示] 意图: $intent ；情绪: $emotion ；
$draft_hint [可用记忆]
$memory

以角色身份直接回复：
"""
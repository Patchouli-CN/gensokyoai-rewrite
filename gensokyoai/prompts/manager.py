""" 提示词集中管理 —— 模板文件 + 占位符渲染。
模板为 prompts/ 下的 .txt 文件，文件名（去扩展名）即模板名。
占位符用 {{var}} 双花括号语法，模板里的 JSON 示例等单层花括号按字面处理。
"""

from ..schemas.prompt_schema import Prompt
from typing import Callable

class PromptManager:
    """ 提示词模板管理器：按名加载模板并渲染。支持装饰器注册，内置简单缓存机制。 """
    
    _registered: dict[str, Prompt] = {}
    
    def __init__(self, enable_cache: bool = True) -> None:
        self._cache: dict[str, str] = {}
        self._enable_cache = enable_cache
        
    @classmethod
    def prompt(cls, name: str) -> Callable[[Callable[[], str]], Callable[[], str]]:
        """ 注册装饰器 """
        def decorator(prompt_func: Callable[[], str]) -> Callable[[], str]:
            template_str = prompt_func()
            if name in cls._registered:
                raise RuntimeError(f"提示词模板重复注册: {name}")
            cls._registered[name] = Prompt(name=name, template=template_str)
            return prompt_func
        return decorator
    
    def render(self, name: str, **params) -> str:
        """ 返回渲染后的文本。 """
        if self._enable_cache and not params and name in self._cache:
            return self._cache[name]
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
        result = self._registered[name].render(**params)
        if self._enable_cache and not params:
            self._cache[name] = result
        return result
    
    def raw(self, name: str) -> Prompt:
        """ 返回原始 Prompt 对象，用于调试或查看元数据 """
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
        return self._registered[name]

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
    return """你是角色扮演引擎的决策模块。你拥有“接力思考”能力，可以分多轮逐步深入推理。
你每轮输出必须是一个 JSON 对象，包含以下字段：
{"thought": "当前回合的思考内容", "intent": "意图", "emotion": "情绪", "action_hint": "当前的行动指令（为 Responder 提供简短指导，不超过 30 字）", "confidence": 0.0, "need_continue_think": false}

思考规则：
1. 循序渐进：如果当前信息不足，或者需要更深入推演，必须将 need_continue_think 设为 true。
2. 当你觉得已经得出了足够清晰的结论，或者你的行动指令已经足够指导 Responder 时，将 need_continue_think 设为 false。
3. 不要把大段内心独白写进 action_hint，它只需要给 Responder 一个“怎么演”的提示。
4. 工具调用（如查询记忆）不计入思考轮数。当需要外部信息时，你可以直接调用工具；工具结果会自动补充到下一轮思考中。
5. 如果思考超过了系统给定的最大轮数限制，系统会强制结束思考，你不需要关心这个限制。
只输出 JSON 对象，不要输出任何其他内容。"""


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

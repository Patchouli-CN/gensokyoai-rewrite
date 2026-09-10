"""提示词集中管理 —— 装饰器注册模板 + 双模渲染（string.Template / 原生 Python）。

模板在本文件内用 `@prompt_mgr.prompt(name)` 装饰器内联注册。
- 无参函数（data 式）：返回含 `$var` 占位符的模板字符串，渲染用 `string.Template.safe_substitute(**params)`;
  未传入的 `$var` 原样保留（不抛 KeyError），字面 `$` 需 `$$` 转义。
- 有参函数（原生式）：接收 `**params`，直接拼字符串——条件/循环/分支全程原生 Python。

装饰器按 `inspect.signature` 判断是否接收参数，两种风格自动共存。
"""

import inspect
from collections.abc import Callable

from ..schemas.prompt_schema import Prompt


class PromptManager:
    """提示词模板管理器：按名渲染。支持装饰器注册、缓存。"""

    _registered: dict[str, Prompt] = {}

    def __init__(self, enable_cache: bool = True) -> None:
        self._cache: dict[str, str] = {}
        self._enable_cache = enable_cache

    @classmethod
    def prompt(cls, name: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
        """注册装饰器：自动识别函数是 data 式（无参）还是原生式（有参）。"""

        def decorator(prompt_func: Callable[..., str]) -> Callable[..., str]:
            if name in cls._registered:
                raise RuntimeError(f"提示词模板重复注册: {name}")
            if inspect.signature(prompt_func).parameters:
                # 原生式：渲染时直接调用函数（不缓存，参数多变）
                cls._registered[name] = Prompt(name=name, renderer=prompt_func)
            else:
                # 数据式：注册期取模板字符串，渲染时 string.Template 替换
                cls._registered[name] = Prompt(name=name, template=prompt_func())
            return prompt_func

        return decorator

    def render(self, name: str, **params) -> str:
        """渲染指定模板。原生式直接传给模板函数，data 式做 $var 替换。"""
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
        prompt = self._registered[name]
        if prompt.renderer is not None:
            return prompt.renderer(**params)
        if self._enable_cache and not params and name in self._cache:
            return self._cache[name]
        result = prompt.render(**params)
        if self._enable_cache and not params:
            self._cache[name] = result
        return result

    def raw(self, name: str) -> Prompt:
        """返回原始 Prompt 对象，用于调试或查看元数据。"""
        if name not in self._registered:
            raise KeyError(f"未找到提示词模板: {name}")
        return self._registered[name]


prompt_mgr = PromptManager()


@prompt_mgr.prompt("brain.think.user")
def brain_think_user(persona, sender, content, context, memory, **_) -> str:
    return (
        f"[人设]\n{persona}\n\n"
        f"[场景] {sender}: {content}\n\n"
        f"[最近上下文]\n{context}\n\n"
        f"[相关记忆]\n{memory}"
    )


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
def ooc_audit_user(persona, draft, **_) -> str:
    return f"[人设]\n{persona}\n\n[回复]\n{draft}"


@prompt_mgr.prompt("ooc.audit")
def ooc_audit() -> str:
    return """你是角色扮演的 OOC 审计员。判断给定回复是否偏离角色人设（是否出戏、是否暴露 AI 身份、是否符合角色性格）。只输出一个 JSON 对象：
{"is_ooc": false, "confidence": 0.0, "reason": "理由"}
"""


@prompt_mgr.prompt("responder.user")
def responder_user(sender, content, intent, emotion, draft_hint, memory, **_) -> str:
    return (
        f"{sender}说: {content}\n"
        f"[决策提示] 意图: {intent}；情绪: {emotion}；\n"
        f"{draft_hint}[可用记忆]\n"
        f"{memory}\n\n"
        f"以角色身份直接回复：\n"
    )


@prompt_mgr.prompt("responder.stall")
def responder_stall(sender, content, **_) -> str:
    return (
        f"{sender}说: {content}\n"
        f"这句话需要认真想一想才能回答。先用角色的口吻回一句简短的过渡语（10~25字），表示你正在思考、回忆或犹豫，可以带一个小动作。\n"
        f"只输出这一句过渡语，保持角色的说话习惯，不要回答内容本身，也不要解释你在做什么。\n"
    )


@prompt_mgr.prompt("responder.correct")
def responder_correct(bad_reply, reason, **_) -> str:
    return (
        f"你刚才的回复出了戏，被系统拦截：\n"
        f"{bad_reply}\n"
        f"原因: {reason}\n"
        f"忘掉它，重新以角色身份自然地回应对方刚才说的话。严禁暴露 AI 身份，严禁跳出角色。\n"
    )

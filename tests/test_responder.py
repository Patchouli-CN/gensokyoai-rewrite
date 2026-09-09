"""Responder 单元测试：半截续写 / 情绪润色 / 会话 system 注入"""

from gensokyoai.core.responder.generator import Responder
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.brain_schema import BrainConclusion, BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult, Message
from gensokyoai.schemas.scene_schema import SceneSnapshot


class ScriptedBackend:
    """按脚本依次返回预设结果的后端"""

    def __init__(self, results: list[CompletionResult]) -> None:
        self._results = list(results)
        self.calls: list[list[Message]] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        return self._results.pop(0)


def _conclusion(emotion: str = "好奇", draft: str | None = None) -> BrainConclusion:
    """标准测试结论"""
    return BrainConclusion(
        intent="闲聊", emotion=emotion, draft=draft, effort=BrainThinkEffort.LOW,
    )


def _snapshot() -> SceneSnapshot:
    """标准测试快照"""
    return SceneSnapshot(sender="小明", content="讲讲红魔馆？", is_direct=True)


async def test_normal_reply_single_call():
    """正常回复只需一次调用"""
    backend = ScriptedBackend([CompletionResult(content="红魔馆是吸血鬼的宅邸。")])
    sm = SessionManager()
    sm.set_default_backend(backend)
    responder = Responder(sm, persona="【测试】\n人设")
    reply = await responder.respond(_conclusion(), _snapshot(), [])
    assert reply == "红魔馆是吸血鬼的宅邸。"
    assert len(backend.calls) == 1
    # system 前缀已注入会话
    assert sm.usage("responder") > 0


async def test_truncated_reply_gets_continuation():
    """finish_reason=length 时发起续写并拼接"""
    backend = ScriptedBackend([
        CompletionResult(content="从前有座山，山里有座", finish_reason="length"),
        CompletionResult(content="红魔馆，馆里住着吸血鬼姐妹。", finish_reason="stop"),
    ])
    sm = SessionManager()
    sm.set_default_backend(backend)
    responder = Responder(sm)
    reply = await responder.respond(_conclusion(), _snapshot(), [])
    assert "从前有座山" in reply and "吸血鬼姐妹" in reply
    assert len(backend.calls) == 2, "截断后应续写一次"
    assert "接续" in backend.calls[1][-1].content, "续写请求应包含接续指令"


async def test_emotion_hint_appends_punctuation():
    """情绪润色：愤怒情绪补感叹号"""
    backend = ScriptedBackend([CompletionResult(content="不许提那个人")])
    sm = SessionManager()
    sm.set_default_backend(backend)
    responder = Responder(sm)
    reply = await responder.respond(_conclusion(emotion="愤怒"), _snapshot(), [])
    assert reply.endswith("！")

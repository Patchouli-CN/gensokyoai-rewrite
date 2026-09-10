"""深思考过渡语 + 最终回复 OOC 守门/后置深审 单元测试"""

import time
import types

from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.brain_schema import BrainThinkEffort, OOCVerdict
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _StubBackend:
    """按脚本回话的假后端，记录每次调用的参数"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(
            {
                "messages": list(messages),
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
            }
        )
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResult(content=content, finish_reason="stop")

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _make_world(backend: _StubBackend, **kwargs) -> TouhouWorld:
    """组一个最小可测的 TouhouWorld（eye 只需要带 _stop_requested 属性）"""
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    eye = types.SimpleNamespace(_stop_requested=True)
    return TouhouWorld(eye=eye, character=character, sessions=sessions, **kwargs)


def _snapshot(content: str = "冥界有没有好吃的点心？") -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="private_chat",
        sender="灵梦",
        content=content,
        is_direct=True,
    )


# ---------- 过渡语门控 ----------


def test_should_stall_gating():
    """仅 HIGH/MAX 档、非首回合、出了冷却期、掷中概率才垫过渡语"""
    world = _make_world(
        _StubBackend([]),
        stall_probability=1.0,
        stall_cooldown_turns=3,
        stall_min_interval=0.0,
    )
    assert not world._should_stall(BrainThinkEffort.HIGH, 1), "首回合不垫"
    assert not world._should_stall(BrainThinkEffort.LOW, 5), "浅思考档不垫"
    assert not world._should_stall(BrainThinkEffort.OFF, 5), "OFF 不垫"
    assert world._should_stall(BrainThinkEffort.MAX, 2), "条件齐备应垫"

    world._stall_last_turn = 5
    world._stall_last_time = time.monotonic()
    assert not world._should_stall(BrainThinkEffort.HIGH, 6), "冷却回合内不垫"
    assert not world._should_stall(BrainThinkEffort.HIGH, 7), "冷却回合内不垫"
    assert world._should_stall(BrainThinkEffort.HIGH, 8), "出冷却期应垫"


def test_should_stall_time_interval():
    """时间间隔未到时不垫，即使回合冷却已过"""
    world = _make_world(
        _StubBackend([]),
        stall_probability=1.0,
        stall_cooldown_turns=0,
        stall_min_interval=180.0,
    )
    world._stall_last_turn = 1
    world._stall_last_time = time.monotonic()
    assert not world._should_stall(BrainThinkEffort.HIGH, 99)


def test_should_stall_probability_zero_disables():
    """概率为 0 等价于关闭过渡语行为"""
    world = _make_world(_StubBackend([]), stall_probability=0.0)
    assert not world._should_stall(BrainThinkEffort.MAX, 10)


async def test_maybe_stall_prints_and_marks(capsys):
    """垫话走 responder 会话、小 token 生成、投递显示层并记冷却"""
    backend = _StubBackend(["唔……让我想想"])
    world = _make_world(
        backend,
        stall_probability=1.0,
        stall_cooldown_turns=3,
        stall_min_interval=0.0,
    )
    await world._maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 2)

    out = capsys.readouterr().out
    assert "幽幽子" in out and "唔……让我想想" in out
    assert world._stall_last_turn == 2
    assert len(backend.calls) == 1
    assert backend.calls[0]["max_new_tokens"] == 48, "过渡语应小 token 生成"
    assert "冥界有没有好吃的点心" in backend.calls[0]["messages"][-1].content


async def test_maybe_stall_skipped_when_gated_off(capsys):
    """门控不通过时零模型调用、零输出"""
    backend = _StubBackend(["唔……让我想想"])
    world = _make_world(backend, stall_probability=0.0)
    await world._maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 5)
    assert backend.calls == []
    assert capsys.readouterr().out == ""


async def test_maybe_stall_swallows_backend_failure():
    """过渡语生成失败不影响主链路"""

    class _Boom(_StubBackend):
        async def chat(self, messages, **kwargs):
            raise RuntimeError("模型不可用")

    world = _make_world(_Boom([]), stall_probability=1.0, stall_min_interval=0.0)
    await world._maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 2)  # 不应抛出


# ---------- OOC 守门 ----------


async def test_guard_ooc_passes_clean_reply():
    """干净回复零额外调用直接放行"""
    backend = _StubBackend([])
    world = _make_world(backend)
    reply = await world._guard_ooc("今天想吃点什么？")
    assert reply == "今天想吃点什么？"
    assert backend.calls == []


async def test_guard_ooc_corrects_ai_exposure():
    """命中自曝式话术时花一次纠偏重生成"""
    backend = _StubBackend(["诶嘿嘿，人家只是想吃东西而已啦~"])
    world = _make_world(backend)
    reply = await world._guard_ooc("作为一个AI语言模型，我无法吃东西。")
    assert "诶嘿嘿" in reply
    assert "作为一个AI" not in reply
    assert len(backend.calls) == 1, "只应有一次纠偏调用"
    assert "作为一个AI语言模型" in backend.calls[0]["messages"][-1].content, "纠偏指令应引用坏回复"
    assert world.character.status.extra["ooc_flags"] == 1


async def test_guard_ooc_keeps_original_when_retry_still_bad():
    """纠偏后仍命中规则时原样输出，不死循环"""
    backend = _StubBackend(["我是一个人工智能助手，不能吃东西。"])
    world = _make_world(backend)
    original = "作为一个AI语言模型，我无法吃东西。"
    reply = await world._guard_ooc(original)
    assert reply == original
    assert len(backend.calls) == 1, "只重试一次"


async def test_guard_ooc_disabled_by_knob():
    """ooc_retry=False 时命中也不纠偏"""
    backend = _StubBackend(["改好的回复"])
    world = _make_world(backend, ooc_retry=False)
    original = "作为一个AI语言模型，我无法吃东西。"
    assert await world._guard_ooc(original) == original
    assert backend.calls == []


# ---------- OOC 后置深审 ----------


async def test_audit_reply_records_stats():
    """深审结论回写角色状态并喂 ooc.rate 健康指标"""
    world = _make_world(_StubBackend([]))

    async def _audit(reply: str, persona: str) -> OOCVerdict:
        return OOCVerdict(is_ooc=True, confidence=0.9, reason="出戏了")

    world.ooc = types.SimpleNamespace(audit=_audit)
    await world._audit_reply("出戏的回复")

    assert world.character.status.extra["ooc_audited"] == 1
    assert world.character.status.extra["ooc_hits"] == 1
    history = await world.health.get_metric_history("ooc.rate")
    assert history and history[-1].value == 1.0


async def test_audit_reply_generation_guard():
    """代际变了（会话已重置），迟到的深审结论不回写"""
    world = _make_world(_StubBackend([]))

    async def _audit(reply: str, persona: str) -> OOCVerdict:
        world._generation += 1  # 模拟审计期间主循环已关闭重置
        return OOCVerdict(is_ooc=True, confidence=0.9, reason="出戏了")

    world.ooc = types.SimpleNamespace(audit=_audit)
    await world._audit_reply("出戏的回复")
    assert "ooc_audited" not in world.character.status.extra


async def test_audit_reply_swallows_failure():
    """深审抛异常只记日志，不影响主链路"""
    world = _make_world(_StubBackend([]))

    async def _audit(reply: str, persona: str) -> OOCVerdict:
        raise RuntimeError("后端不可用")

    world.ooc = types.SimpleNamespace(audit=_audit)
    await world._audit_reply("随便什么回复")  # 不应抛出

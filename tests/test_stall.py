"""StallSpeaker 组件测试 —— 深思考过渡语（三重门控 / 投递 / 冷却记账 / 失败吞掉 / 持久化）。"""

import time

from gensokyoai.roleplay.components.stall import StallSpeaker
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _StubResponder:
    """只实现 stall() 的假表达层：按脚本回话、记录调用次数"""

    def __init__(self, line: str = "唔……让妾身想想") -> None:
        self.line = line
        self.calls = 0

    async def stall(self, snapshot: SceneSnapshot) -> str:
        self.calls += 1
        return self.line


class _StubMouth:
    """假口层：记录投递（send 即最终显示层）"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, speaker: str, text: str) -> None:
        self.sent.append(text)


def _speaker(
    responder: _StubResponder | None = None,
    mouth: _StubMouth | None = None,
    **knobs,
) -> StallSpeaker:
    """组一个过渡语播报器（knobs 直通 probability/cooldown_turns/min_interval）"""
    return StallSpeaker(
        responder or _StubResponder(),
        mouth or _StubMouth(),
        "幽幽子",
        **knobs,
    )


def _snapshot() -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="private_chat",
        sender="灵梦",
        content="冥界有没有好吃的点心？",
        is_direct=True,
    )


def test_should_stall_gating():
    """仅 HIGH/MAX 档、非首回合、出了冷却期、掷中概率才垫过渡语"""
    speaker = _speaker(probability=1.0, cooldown_turns=3, min_interval=0.0)
    assert not speaker.should_stall(BrainThinkEffort.HIGH, 1), "首回合不垫"
    assert not speaker.should_stall(BrainThinkEffort.LOW, 5), "浅思考档不垫"
    assert not speaker.should_stall(BrainThinkEffort.NONE, 5), "NONE 不垫"
    assert speaker.should_stall(BrainThinkEffort.MAX, 2), "条件齐备应垫"

    speaker.last_turn = 5
    speaker._last_time = time.monotonic()
    assert not speaker.should_stall(BrainThinkEffort.HIGH, 6), "冷却回合内不垫"
    assert not speaker.should_stall(BrainThinkEffort.HIGH, 7), "冷却回合内不垫"
    assert speaker.should_stall(BrainThinkEffort.HIGH, 8), "出冷却期应垫"


def test_should_stall_time_interval():
    """时间间隔未到时不垫，即使回合冷却已过"""
    speaker = _speaker(probability=1.0, cooldown_turns=0, min_interval=180.0)
    speaker.last_turn = 1
    speaker._last_time = time.monotonic()
    assert not speaker.should_stall(BrainThinkEffort.HIGH, 99)


def test_should_stall_probability_zero_disables():
    """概率为 0 等价于关闭过渡语行为"""
    speaker = _speaker(probability=0.0)
    assert not speaker.should_stall(BrainThinkEffort.MAX, 10)


async def test_maybe_stall_prints_and_marks():
    """垫话走 responder 会话、投递显示层并记冷却"""
    mouth = _StubMouth()
    responder = _StubResponder("唔……让妾身想想")
    speaker = _speaker(responder, mouth, probability=1.0, cooldown_turns=3, min_interval=0.0)
    await speaker.maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 2)

    assert mouth.sent == ["唔……让妾身想想"]
    assert speaker.last_turn == 2
    assert responder.calls == 1


async def test_maybe_stall_skipped_when_gated_off():
    """门控不通过时零模型调用、零投递"""
    mouth = _StubMouth()
    responder = _StubResponder()
    speaker = _speaker(responder, mouth, probability=0.0)
    await speaker.maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 5)
    assert responder.calls == 0
    assert mouth.sent == []


async def test_maybe_stall_swallows_backend_failure():
    """过渡语生成失败不影响主链路"""

    class _Boom(_StubResponder):
        async def stall(self, snapshot: SceneSnapshot) -> str:
            raise RuntimeError("模型不可用")

    speaker = _speaker(_Boom(), _StubMouth(), probability=1.0, min_interval=0.0)
    await speaker.maybe_stall(_snapshot(), BrainThinkEffort.HIGH, 2)  # 不应抛出


def test_dump_load_roundtrip():
    """持久化：上次垫话回合号往返；monotonic 时刻恢复时重置为现在"""
    speaker = _speaker()
    speaker.last_turn = 12
    assert speaker.dump() == 12

    restored = _speaker()
    restored.load(12)
    assert restored.last_turn == 12
    assert restored._last_time != float("-inf")

    restored.load("not-an-int")
    assert restored.last_turn == 12, "非法值保持原状"

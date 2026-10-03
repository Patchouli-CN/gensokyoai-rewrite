"""精力模型测试（HumanLikeSystem 阶段二）：

三因子（生物钟 / 存在感惩罚 / 冷场退避）各自与连乘、阈值调制边界、
简短提示开关、世界接线（跳过记账 / 阈值入裁判 / 状态提示进 Responder 提示词）。
"""

import types
from datetime import datetime

import pytest

from gensokyoai.core.brain.energy import EnergyModel
from gensokyoai.core.brain.gate import PresenceTracker
from gensokyoai.core.config import EnergySettings, GateSettings
from gensokyoai.core.memorizer.manager import MemoryManager
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.prompts import prompt_mgr
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.components.gate_ctl import System1Gate
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


def _make_model(
    settings: EnergySettings | None = None, *, hour: int = 12
) -> tuple[EnergyModel, PresenceTracker, dict]:
    """组一个时钟/时间都可控的 EnergyModel。"""
    state = {"t": 1000.0}
    presence = PresenceTracker(window_s=300.0, clock=lambda: state["t"])
    model = EnergyModel(
        settings or EnergySettings(enabled=True),
        presence,
        clock=lambda: state["t"],
        now=lambda: datetime(2026, 10, 2, hour, 0, 0),
    )
    return model, presence, state


class _StubBackend:
    """记录调用参数的假模型后端"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append({"messages": list(messages)})
        content = self.replies.pop(0) if self.replies else ""
        return CompletionResult(content=content)

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _FakeJudge:
    """脚本化裁判：返回预设概率"""

    def __init__(self, answers: dict[str, float]) -> None:
        self._answers = answers

    async def ask(self, state, questions):
        return dict(self._answers)


def _group_snapshot(text: str, sender: str = "群友") -> SceneSnapshot:
    return SceneSnapshot(content=text, sender=sender, scene_type="group_chat", is_direct=False)


def _make_world(backend: _StubBackend, **kwargs) -> TouhouWorld:
    """组一个最小可测的 TouhouWorld（eye 只需要带 _stop_requested 属性）"""
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    eye = types.SimpleNamespace(_stop_requested=True)
    return TouhouWorld(eye=eye, character=character, sessions=sessions, **kwargs)


# ---------- 三因子 ----------


def test_fresh_model_has_full_energy():
    """全新状态：没说过话、没冷场、白天 -> 精力满格，阈值不动，无简短提示"""
    model, _, _ = _make_model()
    assert model.energy() == 1.0
    assert model.factors() == (1.0, 1.0, 1.0)
    assert model.modulate_threshold(0.8) == 0.8
    assert model.verbosity_hint() == ""


def test_presence_penalty_kicks_in_over_free_ratio():
    """窗口内自己发言占比超免罚线后线性扣精力；免罚线内不扣"""
    model, presence, _ = _make_model()
    # 1 bot / 4 total = 0.25 = 免罚线，不扣
    for _ in range(3):
        presence.record(from_bot=False)
    presence.record(from_bot=True)
    assert model.factors()[1] == 1.0
    # 再补一条 bot：2/5 = 0.4 -> over=(0.4-0.25)/0.75=0.2 -> 1-0.5*0.2=0.9
    presence.record(from_bot=True)
    assert model.factors()[1] == pytest.approx(0.9)


def test_presence_penalty_saturates():
    """窗口里全是自己在说（占比 100%）-> 扣满 presence_penalty"""
    model, presence, _ = _make_model()
    for _ in range(4):
        presence.record(from_bot=True)
    assert model.factors()[1] == pytest.approx(1.0 - 0.5)


def test_presence_window_expires():
    """存在感统计随窗口过期，精力自动恢复（累了歇会儿就好）"""
    model, presence, state = _make_model()
    for _ in range(4):
        presence.record(from_bot=True)
    assert model.factors()[1] < 1.0
    state["t"] += 301.0  # 越过 300s 窗口
    assert model.factors()[1] == 1.0


def test_skip_backoff_decays_and_reply_resets():
    """冷场退避：裁判/兜底的主动跳过几何衰减，一开口立即复位"""
    model, _, _ = _make_model()
    model.note_skip("judge")
    model.note_skip("judge")
    assert model.skip_streak == 2
    assert model.factors()[2] == pytest.approx(0.85**2)
    model.note_reply()
    assert model.skip_streak == 0
    assert model.factors()[2] == 1.0


def test_rule_skip_does_not_count_toward_backoff():
    """反射弧跳过（收尾语/纯反应）不消耗精力——被「哈哈」跳过不该累"""
    model, _, _ = _make_model()
    model.note_skip("rule")
    assert model.skip_streak == 0
    assert model.energy() == 1.0


def test_skip_streak_capped():
    """冷场连胜封顶，衰减不会无限小"""
    model, _, _ = _make_model()
    for _ in range(20):
        model.note_skip("fallback")
    assert model.skip_streak == 8
    assert model.factors()[2] == pytest.approx(0.85**8)


def test_circadian_dips_at_night():
    """生物钟：深夜 1:00~7:00 精力打折，白天原价"""
    night, _, _ = _make_model(hour=3)
    assert night.factors()[0] == 0.7
    day, _, _ = _make_model(hour=12)
    assert day.factors()[0] == 1.0
    boundary, _, _ = _make_model(hour=7)  # 左闭右开：7 点已天亮
    assert boundary.factors()[0] == 1.0


def test_circadian_wraps_around_midnight():
    """跨午夜时段（23~7）：23 点与凌晨 3 点都困，中午不困"""
    settings = EnergySettings(enabled=True, night_start=23, night_end=7)
    late, _, _ = _make_model(settings, hour=23)
    assert late.factors()[0] == 0.7
    dawn, _, _ = _make_model(settings, hour=3)
    assert dawn.factors()[0] == 0.7
    noon, _, _ = _make_model(settings, hour=12)
    assert noon.factors()[0] == 1.0


def test_factors_multiply():
    """三因子连乘：深夜 + 存在感半扣 + 冷场两连"""
    model, presence, _ = _make_model(hour=3)
    for _ in range(4):
        presence.record(from_bot=True)
    model.note_skip("judge")
    model.note_skip("judge")
    expected = 0.7 * 0.5 * (0.85**2)
    assert model.energy() == pytest.approx(expected, abs=1e-4)


# ---------- 阈值与提示 ----------


def test_threshold_only_lifts_and_caps():
    """阈值调制只抬不压，且封顶 threshold_cap"""
    settings = EnergySettings(enabled=True, threshold_span=0.4, threshold_cap=0.9)
    model, presence, _ = _make_model(settings)
    for _ in range(4):
        presence.record(from_bot=True)  # 存在感半扣 -> energy 0.5
    lifted = model.modulate_threshold(0.8)
    assert lifted == pytest.approx(0.9)  # 0.8 + 0.5*0.4 = 1.0 -> 封顶 0.9
    assert model.modulate_threshold(0.8) >= 0.8


def test_verbosity_hint_only_when_tired():
    """精力低于 brief_below 才给简短提示"""
    settings = EnergySettings(enabled=True, brief_below=0.4)
    model, presence, _ = _make_model(settings)
    assert model.verbosity_hint() == ""
    for _ in range(4):
        presence.record(from_bot=True)  # energy -> 0.5，还没到 0.4 以下
    assert model.verbosity_hint() == ""
    model.note_skip("judge")
    model.note_skip("judge")
    model.note_skip("judge")  # 0.5 * 0.85^3 ≈ 0.31 < 0.4
    hint = model.verbosity_hint()
    assert "简短" in hint


def test_describe_breaks_down_factors():
    """日志拆解包含精力值与三因子"""
    model, _, _ = _make_model()
    text = model.describe()
    assert "energy=1.00" in text
    assert "冷场=0" in text


# ---------- 配置 ----------


def test_energy_settings_defaults_preserve_old_behavior():
    """代码默认关精力：直接构造世界维持旧行为；settings.yaml 显式打开"""
    assert EnergySettings().enabled is False


def test_responder_prompt_renders_state_line():
    """responder.user 提示词：state 非空时出现 [当前状态] 行，为空不出现"""
    base = dict(
        sender="群友",
        content="在吗",
        intent="寒暄",
        emotion="平静",
        draft_hint="",
        memory="（无）",
    )
    with_state = prompt_mgr.render("responder.user", **base, state="精力偏低：长话短说")
    assert "[当前状态] 精力偏低：长话短说" in with_state
    without_state = prompt_mgr.render("responder.user", **base)
    assert "[当前状态]" not in without_state


# ---------- 世界接线 ----------


async def test_world_judge_skip_feeds_energy(tmp_path):
    """门控裁判说「不接」-> 冷场连胜 +1；规则反射弧跳过不加"""
    judge = _FakeJudge({"should_reply": 0.1, "addressed": 0.0, "needs_search": 0.0})
    world = _make_world(
        _StubBackend([]),
        judge=judge,
        gate=GateSettings(enabled=True),
        energy=EnergySettings(enabled=True),
        storage_dir=tmp_path,
    )
    world._presence.record(from_bot=False)
    ok, _ = await world._system1_turn(_group_snapshot("今天天气不错"), 1)
    assert ok is False
    assert world._energy.skip_streak == 1

    # 反射弧跳过（收尾语）：不进裁判、不计冷场
    ok, _ = await world._system1_turn(_group_snapshot("哈哈哈哈"), 2)
    assert ok is False
    assert world._energy.skip_streak == 1


async def test_gate_threshold_modulated_by_energy(tmp_path):
    """精力低 -> 交给裁判的阈值被抬高（打分明细里能看到调制后的阈值）"""
    judge = _FakeJudge({"should_reply": 0.1, "needs_search": 0.0})
    model, presence, _ = _make_model(
        EnergySettings(enabled=True, presence_free_ratio=0.0, presence_penalty=1.0)
    )
    memory = MemoryManager(storage_dir=tmp_path, session_id="energy-gate")
    gate_ctl = System1Gate(
        GateSettings(enabled=True, group_threshold=0.6),
        judge,
        presence,
        model,
        "【幽幽子】白玉楼的主人是也",
        "幽幽子",
        memory,
    )
    # 窗口里全是自己在说 -> 存在感因子 0 -> 精力 0 -> 阈值顶到 0.6+0.2
    for _ in range(4):
        presence.record(from_bot=True)

    await gate_ctl.decide(_group_snapshot("随便聊聊"), 1)

    assert model.energy() == 0.0
    decision = await gate_ctl.decide(_group_snapshot("再聊聊"), 2)
    assert decision.scores.threshold == pytest.approx(0.8)


# 精力对投递的影响（状态提示注入 / 开口复位）见 test_delivery.py（DeliveryService 组件）

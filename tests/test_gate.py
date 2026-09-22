"""JeV 式发言门控测试：规则预筛 / 裁判打分 / 兜底 / 两个裁判后端 / 世界接线"""

import asyncio
import types

import pytest

from gensokyoai.core.brain import judge as judge_module
from gensokyoai.core.brain.gate import (
    PresenceTracker,
    Question,
    build_questions,
    build_state,
    decide,
    is_bare_reaction,
    tier_from_deep_score,
)
from gensokyoai.core.brain.judge import (
    LocalJudge,
    TypeSafeJudge,
    build_judge,
    parse_probabilities,
)
from gensokyoai.core.config import GateSettings
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.prompts import prompt_mgr
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot


class _StubBackend:
    """记录调用参数的假模型后端"""

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
        return CompletionResult(content=content)

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


class _FakeJudge:
    """脚本化裁判：记录收到的 state，返回预设概率（或模拟宕机）"""

    def __init__(self, answers: dict[str, float] | None = None, *, boom: bool = False) -> None:
        self._answers = answers if answers is not None else {}
        self._boom = boom
        self.state: dict | None = None

    async def ask(self, state, questions):
        self.state = state
        if self._boom:
            raise RuntimeError("裁判宕机")
        return self._answers


def _group_snapshot(text: str, **kw) -> SceneSnapshot:
    return SceneSnapshot(scene_type="group_chat", sender="灵梦", content=text, **kw)


def _private_snapshot(text: str) -> SceneSnapshot:
    return SceneSnapshot(scene_type="private_chat", sender="魔理沙", content=text, is_direct=True)


def _decide_kwargs(**overrides):
    """decide() 的常用基线参数"""
    base = dict(
        snapshot=_group_snapshot("今天的异变有点奇怪"),
        bot_name="幽幽子",
        persona="白玉楼的主人",
        recent=["灵梦: 大家早上好", "魔理沙: 早"],
        presence=(1, 4, 20.0),
        judge=None,
        group_threshold=0.6,
        search_threshold=0.1,
    )
    base.update(overrides)
    return base


# ---------- 收尾语识别 ----------


def test_is_bare_reaction_recognizes_closings():
    """收尾语 / 纯反应全部判「无需回应」"""
    for text in ["哈哈哈", "@小猫 草", "666", "好的！", "嗯嗯", "ok", "谢谢~", "[表情包]", "？？"]:
        assert is_bare_reaction(text) is True, text


def test_is_bare_reaction_rejects_real_talk():
    """带内容的句子不是收尾语（别把「哈哈哈这个好笑在哪」误杀）"""
    for text in ["哈哈哈这个好笑在哪", "好的，那明天几点", "6点见", "为什么"]:
        assert is_bare_reaction(text) is False, text


# ---------- 活跃度统计 ----------


def test_presence_tracker_counts_and_prunes():
    """窗口内计数 + 过期淘汰 + 距上次 bot 发言秒数"""
    now = {"t": 1000.0}
    tracker = PresenceTracker(window_s=300.0, clock=lambda: now["t"])
    assert tracker.stats() == (0, 0, None)

    tracker.record(from_bot=False)
    tracker.record(from_bot=True)
    now["t"] += 10
    tracker.record(from_bot=False)
    assert tracker.stats() == (1, 3, 10.0)

    now["t"] += 250  # 距上次 bot 发言 260s，仍在 300s 窗口内
    assert tracker.stats() == (1, 3, 260.0)

    now["t"] += 50  # 1310s：窗口下沿 1010，t=1000 的两条滚出，t=1010 的留下
    assert tracker.stats() == (0, 1, None)


# ---------- 规则预筛 ----------


async def test_decide_rules_skip_bare_reaction():
    """收尾语零成本跳过，不调裁判"""
    judge = _FakeJudge({"should_reply": 0.99})
    decision = await decide(**_decide_kwargs(snapshot=_group_snapshot("哈哈哈哈"), judge=judge))
    assert decision.reply is False
    assert decision.source == "rule"
    assert judge.state is None, "规则直判不应惊动裁判"


async def test_decide_rules_reply_private_and_direct():
    """私聊与被 @ 由规则直判放行（明显信号不花裁判）"""
    judge = _FakeJudge({"should_reply": 0.01})
    private = await decide(**_decide_kwargs(snapshot=_private_snapshot("在吗"), judge=judge))
    assert private.reply is True and private.source == "rule"

    direct = await decide(
        **_decide_kwargs(snapshot=_group_snapshot("@幽幽子 在吗", is_direct=True), judge=judge)
    )
    assert direct.reply is True and direct.source == "rule"
    assert judge.state is None


# ---------- 裁判路径 ----------


async def test_decide_judge_threshold_decides():
    """群聊模糊带由裁判打分：过阈值发言、不过则沉默"""
    high = await decide(
        **_decide_kwargs(
            judge=_FakeJudge({"should_reply": 0.72, "addressed": 0.9, "needs_search": 0.2})
        )
    )
    assert high.reply is True
    assert high.source == "judge"
    assert high.scores is not None
    assert high.scores.reply == pytest.approx(0.72)
    assert high.scores.threshold == pytest.approx(0.6)
    assert high.search is True, "needs_search 0.2 > 0.1 应挂搜索意图"

    low = await decide(**_decide_kwargs(judge=_FakeJudge({"should_reply": 0.5, "addressed": 0.2})))
    assert low.reply is False
    assert low.search is False


async def test_decide_judge_receives_state_with_presence():
    """裁判吃到的 state 带活跃度与现场（防刷屏靠它自觉）"""
    judge = _FakeJudge({"should_reply": 0.61})
    decision = await decide(
        **_decide_kwargs(
            snapshot=_group_snapshot("异变来了"),
            recent=["灵梦: 有异变"],
            presence=(3, 12, 5.0),
            judge=judge,
        )
    )
    assert decision.reply is True
    assert judge.state is not None
    assert judge.state["bot_activity"]["bot_messages_last_window"] == 3
    assert judge.state["bot_activity"]["all_messages_last_window"] == 12
    assert judge.state["new_message"]["from"] == "灵梦"
    assert judge.state["recent_messages"] == ["灵梦: 有异变"]


async def test_decide_judge_error_falls_back_to_named():
    """裁判异常不死链路：点名才回，群聊闲聊不接"""
    named = await decide(
        **_decide_kwargs(snapshot=_group_snapshot("幽幽子你看这个"), judge=_FakeJudge(boom=True))
    )
    assert named.reply is True
    assert named.source == "fallback"
    assert "裁判异常" in named.reason

    chatter = await decide(
        **_decide_kwargs(snapshot=_group_snapshot("今天真热啊"), judge=_FakeJudge(boom=True))
    )
    assert chatter.reply is False
    assert chatter.source == "fallback"


async def test_decide_without_judge_only_named_replies():
    """无裁判兜底：名字出现在最近对话里也算被点名"""
    mentioned = await decide(
        **_decide_kwargs(snapshot=_group_snapshot("有人算我吗"), recent=["灵梦: 幽幽子出来"])
    )
    assert mentioned.reply is True
    plain = await decide(**_decide_kwargs(snapshot=_group_snapshot("有人算我吗")))
    assert plain.reply is False


# ---------- 档位模型化路由 ----------


async def test_decide_route_by_model_asks_judge_for_direct():
    """route_by_model=True：私聊/被@ 也问裁判（回复仍由规则直判，打分仅供路由）"""
    judge = _FakeJudge({"should_reply": 0.01, "needs_deep": 0.95})
    decision = await decide(
        **_decide_kwargs(snapshot=_private_snapshot("在吗"), judge=judge, route_by_model=True)
    )
    assert decision.reply is True, "直连信号优先，裁判的 should_reply=0.01 不作数"
    assert decision.source == "judge"
    assert "直连信号" in decision.reason
    assert decision.effort == pytest.approx(0.95)
    assert judge.state is not None, "为了路由，裁判确实被问了"


async def test_decide_route_off_skips_judge_for_direct():
    """route_by_model=False（默认）：私聊/被@ 不问裁判，规则直判（旧行为）"""
    judge = _FakeJudge({"should_reply": 0.01, "needs_deep": 0.95})
    decision = await decide(**_decide_kwargs(snapshot=_private_snapshot("在吗"), judge=judge))
    assert decision.reply is True and decision.source == "rule"
    assert decision.effort is None
    assert judge.state is None


async def test_decide_missing_deep_score_means_no_routing_info():
    """裁判没答 needs_deep -> effort=None，调用方回落规则路由"""
    judge = _FakeJudge({"should_reply": 0.8, "addressed": 0.5})
    decision = await decide(**_decide_kwargs(judge=judge, route_by_model=True))
    assert decision.reply is True
    assert decision.effort is None


async def test_decide_judge_error_keeps_direct_reply():
    """裁判故障时直连信号仍然放行（路由信息丢失 -> 回落规则）"""
    decision = await decide(
        **_decide_kwargs(
            snapshot=_private_snapshot("在吗"), judge=_FakeJudge(boom=True), route_by_model=True
        )
    )
    assert decision.reply is True
    assert decision.source == "rule"
    assert "裁判异常" in decision.reason


def test_tier_from_deep_score_boundaries():
    """分数 -> 档位的三个切点"""
    assert tier_from_deep_score(0.0) is BrainThinkEffort.LOW
    assert tier_from_deep_score(0.29) is BrainThinkEffort.LOW
    assert tier_from_deep_score(0.3) is BrainThinkEffort.MID
    assert tier_from_deep_score(0.59) is BrainThinkEffort.MID
    assert tier_from_deep_score(0.6) is BrainThinkEffort.HIGH
    assert tier_from_deep_score(0.84) is BrainThinkEffort.HIGH
    assert tier_from_deep_score(0.85) is BrainThinkEffort.MAX
    assert tier_from_deep_score(1.0) is BrainThinkEffort.MAX
    # 自定义切点
    assert tier_from_deep_score(0.5, cuts=(0.5, 0.7, 0.9)) is BrainThinkEffort.MID


def test_gate_settings_route_defaults():
    """路由开关默认开 + 切点单调"""
    settings = GateSettings()
    assert settings.route_by_model is True
    mid, high, mx = settings.deep_cuts
    assert 0 < mid < high < mx < 1


# ---------- state / questions 构造 ----------


def test_build_state_feeds_judge_activity_and_flags():
    """state 结构对齐 qqbot 的 buildState（chat/bot/bot_activity/recent/new）"""
    state = build_state(
        snapshot=_group_snapshot("有人在吗"),
        bot_name="幽幽子",
        persona="白玉楼的主人",
        recent=["灵梦: 大家早上好", "魔理沙: 早"],
        presence=(2, 9, 30.0),
    )
    assert state["chat"] == "群聊"
    assert state["bot"] == {"name": "幽幽子", "persona": "白玉楼的主人"}
    assert state["bot_activity"] == {
        "bot_messages_last_window": 2,
        "all_messages_last_window": 9,
        "seconds_since_bot_last_spoke": 30,
    }
    assert state["new_message"] == {
        "from": "灵梦",
        "text": "有人在吗",
        "directly_addressed": False,
    }


def test_build_questions_four_dimensions():
    """System-1 四问：该不该回 / 是不是在说角色 / 要不要查证 / 想多深"""
    questions = build_questions("幽幽子")
    assert set(questions) == {
        "should_reply",
        "addressed",
        "needs_search",
        "needs_deep",
    }
    assert "幽幽子" in questions["should_reply"].instructions
    assert all(isinstance(q, Question) for q in questions.values())


# ---------- LocalJudge ----------


async def test_local_judge_parses_stateless_small_call():
    """本地裁判：解析 JSON 概率，无状态小调用（低温 / 小 max_new_tokens / 不落会话）"""
    backend = _StubBackend(['{"should_reply": 0.72, "addressed": 0.4, "needs_search": 0.05}'])
    sessions = SessionManager()
    sessions.set_default_backend(backend)

    answers = await LocalJudge(sessions).ask({"chat": "群聊"}, build_questions("幽幽子"))

    assert answers["should_reply"] == pytest.approx(0.72)
    assert len(backend.calls) == 1
    call = backend.calls[0]
    assert call["max_new_tokens"] == 128
    assert call["temperature"] == 0.2
    assert call["messages"][0].role == "system"
    assert "should_reply" in call["messages"][-1].content
    assert sessions.usage("gate.think") == 0, "无状态调用不落会话历史"


async def test_local_judge_raises_on_malformed_output():
    """模型不说 JSON 时抛错，让 decide 走兜底"""
    sessions = SessionManager()
    sessions.set_default_backend(_StubBackend(["今天天气不错，适合发呆"]))
    with pytest.raises(ValueError):
        await LocalJudge(sessions).ask({}, build_questions("幽幽子"))


async def test_local_judge_times_out():
    """超时保护：慢模型不会把主循环拖死"""

    class _SlowBackend:
        async def chat(self, messages, **kwargs):
            await asyncio.sleep(10)
            return CompletionResult(content="{}")

        def normalize_tool_calls(self, result, parsed_content=None):
            return result

    sessions = SessionManager()
    sessions.set_default_backend(_SlowBackend())
    with pytest.raises(asyncio.TimeoutError):
        await LocalJudge(sessions, timeout_s=0.05).ask({}, build_questions("幽幽子"))


def test_parse_probabilities_tolerates_noise_and_clamps():
    """容忍 ```json 包裹；越界值夹到 0~1"""
    text = '```json\n{"should_reply": 1.8, "addressed": -2, "needs_search": 0.5}\n```'
    answers = parse_probabilities(text, build_questions("幽幽子"))
    assert answers["should_reply"] == 1.0
    assert answers["addressed"] == 0.0
    assert answers["needs_search"] == 0.5


def test_parse_probabilities_ignores_non_numeric():
    """布尔/字符串不算概率；一个题都取不到时抛错（交给 decide 兜底）"""
    with pytest.raises(ValueError):
        parse_probabilities(
            '{"should_reply": true, "addressed": "高"}',
            {"should_reply": Question("x"), "addressed": Question("y")},
        )


def test_parse_probabilities_rejects_garbage():
    """一个题都取不到时抛错"""
    with pytest.raises(ValueError):
        parse_probabilities("模型不想回答", {"should_reply": Question("x")})


# ---------- TypeSafeJudge ----------


class _Noul:
    """typesafe_sdk.Noul 的替身"""

    def __init__(self, instructions: str) -> None:
        self.instructions = instructions


class _Answer:
    def __init__(self, noul: float) -> None:
        self.noul = noul


class _FakeTypeSafeClient:
    """记录调用的假 TypeSafe 客户端"""

    def __init__(self, answers: dict[str, float]) -> None:
        self._answers = answers
        self.state = None
        self.questions = None

    async def system_one(self, *, state, questions):
        self.state = state
        self.questions = questions
        return types.SimpleNamespace(
            answers={name: _Answer(value) for name, value in self._answers.items()}
        )


def _patch_typesafe(monkeypatch) -> None:
    monkeypatch.setattr(judge_module, "_load_typesafe", lambda: types.SimpleNamespace(Noul=_Noul))


def test_typesafe_judge_extracts_probabilities(monkeypatch):
    """真 jev：Noul 为题、取 answers 里的概率"""
    _patch_typesafe(monkeypatch)
    client = _FakeTypeSafeClient({"should_reply": 0.82, "addressed": 0.4})

    judge = TypeSafeJudge(client=client)
    answers = asyncio.run(judge.ask({"chat": "群聊"}, build_questions("幽幽子")))

    assert answers["should_reply"] == pytest.approx(0.82)
    assert answers["addressed"] == pytest.approx(0.4)
    assert set(client.questions) == {
        "should_reply",
        "addressed",
        "needs_search",
        "needs_deep",
    }
    assert all(isinstance(q, _Noul) for q in client.questions.values())


def test_typesafe_judge_raises_when_sdk_missing(monkeypatch):
    """未装 typesafe-sdk 时给出可操作的报错（而不是 ImportError 栈）"""

    def _fake_import_module(name):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(judge_module.importlib, "import_module", _fake_import_module)
    with pytest.raises(RuntimeError, match="typesafe-sdk"):
        asyncio.run(TypeSafeJudge().ask({}, build_questions("幽幽子")))


def test_typesafe_judge_raises_on_empty_answers(monkeypatch):
    """jev 没返回可用概率时抛错，交给兜底"""
    _patch_typesafe(monkeypatch)
    client = _FakeTypeSafeClient({})
    with pytest.raises(ValueError):
        asyncio.run(TypeSafeJudge(client=client).ask({}, build_questions("幽幽子")))


# ---------- 装配与配置 ----------


def test_build_judge_dispatch():
    """judge 配置决定后端；none/未知 -> None（纯规则+兜底）"""
    sessions = SessionManager()
    assert build_judge(GateSettings(judge="none"), sessions) is None
    assert build_judge(GateSettings(judge="未知"), sessions) is None
    assert isinstance(build_judge(GateSettings(judge="local"), sessions), LocalJudge)
    assert isinstance(build_judge(GateSettings(judge="typesafe"), sessions), TypeSafeJudge)


def test_gate_settings_defaults_preserve_old_behavior():
    """代码默认关门：直接构造世界维持「每条都回」；settings.yaml 显式打开"""
    settings = GateSettings()
    assert settings.enabled is False
    assert settings.group_threshold == 0.6


def test_gate_prompts_render():
    """门控提示词已注册且可渲染"""
    system = prompt_mgr.render("gate.system")
    assert "should_reply" in system
    assert "只输出 JSON" in system
    user = prompt_mgr.render("gate.user", state='{"chat": "群聊"}', questions="- should_reply：……")
    assert '"chat": "群聊"' in user
    assert "should_reply" in user


# ---------- 世界接线 ----------


def _make_world(backend: _StubBackend, **kwargs) -> TouhouWorld:
    """组一个最小可测的 TouhouWorld（eye 只需要带 _stop_requested 属性）"""
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    character = Character(CharacterCard(name="幽幽子", system_prompt="白玉楼的主人是也"))
    eye = types.SimpleNamespace(_stop_requested=True)
    return TouhouWorld(eye=eye, character=character, sessions=sessions, **kwargs)


async def test_world_gate_skips_and_still_stores_user_message(tmp_path):
    """门控说「不接」：不进 Brain，但用户消息仍然入记忆（不回复≠没听过）"""
    judge = _FakeJudge({"should_reply": 0.1, "addressed": 0.9, "needs_search": 0.0})
    world = _make_world(
        _StubBackend([]),
        judge=judge,
        gate=GateSettings(enabled=True),
        storage_dir=tmp_path,
    )

    world._presence.record(from_bot=False)
    ok, judged = await world._system1_turn(_group_snapshot("今天天气不错"), 1)

    assert ok is False
    assert judged is None
    await world._tasks.drain(timeout=1.0)
    recent = await world.memory.recent(5)
    assert any("今天天气不错" in m.content for m in recent)
    history = await world.health.get_metric_history("gate.skip")
    assert history and history[-1].value == 1.0


async def test_world_gate_passes_through(tmp_path):
    """门控放行进 Brain；裁判拿到的是人设摘要而不是整本提示词（控 token）"""
    judge = _FakeJudge({"should_reply": 0.9, "addressed": 0.2, "needs_search": 0.0})
    world = _make_world(
        _StubBackend([]),
        judge=judge,
        gate=GateSettings(enabled=True),
        storage_dir=tmp_path,
    )

    ok, judged = await world._system1_turn(_group_snapshot("幽幽子来看新泳装"), 2)

    assert ok is True
    assert judge.state is not None
    assert judge.state["bot"]["persona"] == world._persona_brief
    assert len(world._persona_brief) < 260
    # 裁判没给 needs_deep -> 回落规则路由
    assert judged is None


async def test_world_model_routing_maps_deep_score_to_tier(tmp_path):
    """模型路由：裁判的 needs_deep 分数经切点映射为推理档位"""
    judge = _FakeJudge(
        {"should_reply": 0.9, "needs_deep": 0.9}  # >= 0.85 -> MAX
    )
    world = _make_world(
        _StubBackend([]),
        judge=judge,
        gate=GateSettings(enabled=True),
        storage_dir=tmp_path,
    )

    ok, judged = await world._system1_turn(_group_snapshot("你还记得西行妖的约定吗"), 3)

    assert ok is True
    assert judged is BrainThinkEffort.MAX


async def test_world_routing_works_when_gate_disabled(tmp_path):
    """门控关着但模型路由开着：照常回复，但档位仍由裁判说了算"""
    judge = _FakeJudge({"should_reply": 0.05, "needs_deep": 0.5})  # 0.3<=0.5<0.6 -> MID
    world = _make_world(
        _StubBackend([]),
        judge=judge,
        gate=GateSettings(enabled=False, route_by_model=True),
        storage_dir=tmp_path,
    )

    ok, judged = await world._system1_turn(_group_snapshot("随便聊聊"), 4)

    assert ok is True, "门控关闭 = 不拦截发言（旧行为）"
    assert judged is BrainThinkEffort.MID
    # 没被拦截：不写 skip 记忆、不喂 skip 计数
    history = await world.health.get_metric_history("gate.skip")
    assert history == []


async def test_world_without_judge_never_asks(tmp_path):
    """没有裁判：System-1 层静默跳过，全程零模型调用、不拦截"""
    world = _make_world(
        _StubBackend([]),
        gate=GateSettings(enabled=True, route_by_model=True),
        storage_dir=tmp_path,
    )

    ok, judged = await world._system1_turn(_group_snapshot("今天天气不错"), 5)

    assert ok is True and judged is None


async def test_world_gate_disabled_by_default(tmp_path):
    """默认关门：不传 gate/judge 时维持旧行为（每条都回）"""
    world = _make_world(_StubBackend([]), storage_dir=tmp_path)
    assert world._gate.enabled is False
    assert world._judge is None

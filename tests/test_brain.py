"""Brain 决策层单元测试：档位路由 / 快速路径 / 接力思考 / OOC 快筛"""

from gensokyoai.core.brain.engine import BrainEngine, route
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.schemas.brain_schema import BrainConclusion, BrainThinkEffort
from gensokyoai.schemas.memory_schema import MemoryItem
from gensokyoai.schemas.model_schema import CompletionResult, Message
from gensokyoai.schemas.scene_schema import SceneSnapshot


class FakeBackend:
    """返回预设内容的假模型后端"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[Message]] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        return CompletionResult(content=self.content)


def _manager(content: str) -> SessionManager:
    """带默认假后端的管理器"""
    sm = SessionManager()
    sm.set_default_backend(FakeBackend(content))
    return sm


def _think_json(action_hint: str = "你要去哪？", need_continue: bool = False) -> str:
    """brain.think 接力协议的单轮 JSON 输出"""
    return (
        '{"thought": "分析用户输入", "intent": "问路", "emotion": "好奇", '
        f'"action_hint": "{action_hint}", "confidence": 0.8, '
        f'"need_continue_think": {"true" if need_continue else "false"}}}'
    )


def _snapshot(text: str = "你好啊", **kw) -> SceneSnapshot:
    return SceneSnapshot(sender="测试员", content=text, **kw)


def test_route_greeting_is_low():
    """短寒暄走 LOW"""
    assert route(_snapshot("你好啊")) is BrainThinkEffort.LOW


def test_route_philosophy_is_high():
    """世界观提问走 HIGH（对齐架构文档 §9.2 示例）"""
    assert route(_snapshot("你觉得这个世界的本质是什么？")) is BrainThinkEffort.HIGH


def test_route_empty_is_off():
    """空内容走 OFF"""
    assert route(_snapshot("")) is BrainThinkEffort.OFF


def test_get_max_rounds_bounded():
    """思考接力上限封闭，不出现 999 类打转值；LOW 至少 2 给工具留消化空间"""
    engine = BrainEngine(SessionManager())
    assert engine._get_max_rounds(BrainThinkEffort.LOW) == 2
    assert engine._get_max_rounds(BrainThinkEffort.MAX) <= 8, "MAX 应封闭上限，不再无限打转"


async def test_think_off_uses_fast_path_without_model_call():
    """OFF 档零模型调用，走快速路径透传"""
    backend = FakeBackend('{"thought": "x"}')
    sm = SessionManager()
    sm.set_default_backend(backend)
    engine = BrainEngine(sm)
    conclusion = await engine.think(_snapshot("你好啊"), [], BrainThinkEffort.OFF)
    assert not backend.calls
    assert conclusion.verdict == "pass_through"
    assert conclusion.intent == "回应打招呼"


async def test_think_parses_model_json_conclusion():
    """模型输出接力 JSON 时正确产出带行动指令的结论"""
    engine = BrainEngine(_manager(_think_json()))
    conclusion = await engine.think(_snapshot("怎么去红魔馆？"), [], BrainThinkEffort.MID)
    assert conclusion.verdict == "draft"
    assert conclusion.intent == "问路"
    assert conclusion.draft == "你要去哪？"
    assert conclusion.reasoning == "分析用户输入"
    # 无状态调用：不落会话
    assert engine._sessions.usage("brain.think") == 0


def test_conclusion_reasoning_defaults_to_none():
    """两种思考都默认为 None（且 raw 与工程实现互不污染）"""
    conclusion = BrainConclusion()
    assert conclusion.reasoning is None
    assert conclusion.raw_reasoning is None


async def test_conclusion_separates_engine_and_raw_reasoning():
    """两种「思考」分开存：工程实现（接力协议）与模型原生 thinking"""

    class _ThinkingBackend:
        async def chat(self, messages, **kw):
            return CompletionResult(content=_think_json(), reasoning="模型原生想了想")

    sm = SessionManager()
    sm.set_default_backend(_ThinkingBackend())
    engine = BrainEngine(sm)
    conclusion = await engine.think(_snapshot("随便聊聊"), [], BrainThinkEffort.LOW)

    assert conclusion.reasoning == "分析用户输入", "工程实现：接力协议里的 thought"
    assert conclusion.raw_reasoning == "模型原生想了想", "模型原生 thinking"


async def test_raw_reasoning_accumulates_across_relay_rounds():
    """多轮接力时，每轮的原生 thinking 逐轮拼接（工程实现只留最后一轮）"""
    scripted = [
        CompletionResult(content=_think_json(need_continue=True), reasoning="第一轮原生"),
        CompletionResult(content=_think_json(need_continue=False), reasoning="第二轮原生"),
    ]
    calls = {"n": 0}

    class _Scripted:
        async def chat(self, messages, **kw):
            result = scripted[calls["n"]]
            calls["n"] += 1
            return result

    sm = SessionManager()
    sm.set_default_backend(_Scripted())
    conclusion = await BrainEngine(sm).think(_snapshot("聊聊"), [], BrainThinkEffort.LOW)

    assert calls["n"] == 2, "应接力两轮"
    assert conclusion.raw_reasoning == "第一轮原生\n第二轮原生"
    assert conclusion.reasoning == "分析用户输入", "工程实现只保留收束轮的 thought"


async def test_conclusion_records_reasoning_steps_per_round():
    """每轮思考都留下 ReasoningStep 快照（轨迹留档的原始素材）"""
    scripted = [
        CompletionResult(content=_think_json(action_hint="先查记忆", need_continue=True)),
        CompletionResult(content=_think_json(action_hint="可以答了", need_continue=False)),
    ]
    calls = {"n": 0}

    class _Scripted:
        async def chat(self, messages, **kw):
            result = scripted[calls["n"]]
            calls["n"] += 1
            return result

    sm = SessionManager()
    sm.set_default_backend(_Scripted())
    conclusion = await BrainEngine(sm).think(_snapshot("聊聊"), [], BrainThinkEffort.LOW)

    steps = conclusion.reasoning_steps
    assert [s.round for s in steps] == [1, 2], "逐轮一条，轮次从 1 起"
    assert steps[0].thought == "分析用户输入"
    assert steps[0].need_continue_think is True
    assert steps[0].action_hint == "先查记忆"
    assert steps[0].intent == "问路"
    assert steps[0].emotion == "好奇"
    assert steps[0].confidence == 0.8
    assert steps[1].need_continue_think is False
    assert steps[1].action_hint == "可以答了"


async def test_off_effort_has_no_reasoning_steps():
    """OFF 快速路径不走接力思考，故无步骤记录"""
    conclusion = await BrainEngine(SessionManager()).think(
        _snapshot("你好啊"), [], BrainThinkEffort.OFF
    )
    assert conclusion.reasoning_steps == []
    assert conclusion.reasoning is None


async def test_relay_think_continues_then_stops():
    """need_continue_think=true 时接力下一轮，false 时收束"""
    sm = SessionManager()
    backend = FakeBackend("")
    sm.set_default_backend(backend)
    engine = BrainEngine(sm)
    # 脚本化：第一轮要求继续，第二轮收束
    scripted = [
        CompletionResult(content=_think_json(need_continue=True)),
        CompletionResult(content=_think_json(need_continue=False)),
    ]

    async def scripted_chat(messages, **kw):
        backend.calls.append(list(messages))
        return scripted[len(backend.calls) - 1]

    backend.chat = scripted_chat

    conclusion = await engine.think(_snapshot("聊聊"), [], BrainThinkEffort.LOW)
    assert len(backend.calls) == 2, "应接力两轮"
    assert any("第1轮思考" in m.content for m in backend.calls[1]), "第二轮应带上第一轮思考上下文"
    assert conclusion.verdict == "draft"


async def test_think_downgrades_on_bad_json():
    """模型输出持续无法解析时降级为快速路径"""
    engine = BrainEngine(_manager("这不是JSON"))
    conclusion = await engine.think(_snapshot("随便聊聊"), [], BrainThinkEffort.LOW)
    assert conclusion.verdict == "pass_through"
    assert conclusion.effort is BrainThinkEffort.LOW


async def test_think_ooc_filter_drops_draft():
    """行动指令命中 OOC 规则时丢弃并打标"""
    engine = BrainEngine(
        _manager(_think_json(action_hint="作为一个AI语言模型我无法回答")),
        persona="测试人设",
        ooc=OOCDetector(SessionManager()),
    )
    conclusion = await engine.think(_snapshot("讲个故事"), [], BrainThinkEffort.LOW)
    assert conclusion.ooc_flag is True
    assert conclusion.draft is None
    assert conclusion.verdict == "pass_through"


async def test_ooc_pre_filter_patterns():
    """OOC 规则快筛命中与放行"""
    detector = OOCDetector(SessionManager())
    assert detector.pre_filter("作为一个AI, 我没有感情").is_ooc is True
    assert detector.pre_filter("哟，又来红魔馆蹭饭？").is_ooc is False


async def test_memory_refs_passed_through_conclusion():
    """记忆条目随结论回传（memory_refs）"""
    engine = BrainEngine(_manager(_think_json()))
    item = MemoryItem(topic="对话", content="她住在雾之湖")
    conclusion = await engine.think(_snapshot("魔理沙住哪？"), [item], BrainThinkEffort.LOW)
    assert item in conclusion.memory_refs

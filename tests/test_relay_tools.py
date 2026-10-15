"""接力思考 / 思考链的工具链路回归：重复拼接 / 跑满轮数 / 坏一轮丢结果 / 伪工具调用 /
工具轮与纠偏额度 / 配置透传 / OOC 预筛范围 / owner 路由 / 门控兜底点名。"""

from gensokyoai.core.brain.engine import BrainEngine
from gensokyoai.core.brain.gate import _fallback
from gensokyoai.core.brain.ooc_detector import OOCDetector
from gensokyoai.core.brain.pipeline import ThinkPipeline, ThinkStep
from gensokyoai.core.registry import ToolRegistry
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.core.toolkit import known_calls
from gensokyoai.models.llama_cpp import extract_text_tool_calls
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult, ToolCall, ToolSpec
from gensokyoai.schemas.scene_schema import SceneSnapshot


def _lookup() -> str:
    """查资料（测试工具）"""
    return "幻想乡的结界由博丽神社维护"


_TOOL = ToolSpec(tool_func=_lookup, desc="查资料", name="lookup")
_TOOL_CALL = "调用 lookup {}"
""" 喊话片段（放进 JSON 字符串值里，不能带未转义的引号） """
_CONCLUSION = (
    '{"verdict": "draft", "intent": "i", "emotion": "e", "draft": "嗯", "confidence": 0.8}'
)


def _think(*, hint: str = "回应", cont: bool = False, thought: str = "想") -> str:
    return (
        f'{{"thought": "{thought}", "intent": "回应", "emotion": "平静", '
        f'"action_hint": "{hint}", "confidence": 0.8, '
        f'"need_continue_think": {"true" if cont else "false"}}}'
    )


class _Scripted:
    """按序回话、按喊话识别工具调用（镜像 LlamaProvider：从解析后的 thought 提取）"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list] = []

    async def chat(self, messages, **kw):
        self.calls.append(list(messages))
        return CompletionResult(content=self.replies.pop(0) if self.replies else _think())

    def normalize_tool_calls(self, result, parsed_content=None):
        if result.tool_calls:
            return result
        text = result.content or ""
        if parsed_content:
            text = f"{parsed_content.get('thought', '')} {parsed_content.get('note', '')}"
        calls = extract_text_tool_calls(text)
        return CompletionResult(content=result.content, tool_calls=calls) if calls else result


def _engine(replies: list[str], **kw) -> tuple[BrainEngine, _Scripted]:
    backend = _Scripted(replies)
    sm = SessionManager()
    sm.set_default_backend(backend)
    return BrainEngine(sm, tools=[_TOOL], **kw), backend


def _snap(text: str = "结界是谁维护的？") -> SceneSnapshot:
    return SceneSnapshot(sender="灵梦", content=text)


async def test_last_round_tool_result_embedded_once():
    """最后一轮才调工具：工具结果只嵌进行动指令一次（此前两处各拼一次）"""
    engine, _ = _engine(
        [_think(cont=True), _think(thought=f"查一下 {_TOOL_CALL}")],
    )
    conclusion = await engine.think(_snap(), [], BrainThinkEffort.LOW)
    assert conclusion.draft is not None
    assert conclusion.draft.count("博丽神社") == 1, conclusion.draft


async def test_tool_digested_round_may_stop():
    """调完工具的下一轮已读到结果、说收尾 -> 收尾，不再被强制跑到轮数上限"""
    engine, backend = _engine([_think(thought=f"查 {_TOOL_CALL}"), _think(hint="答案是博丽神社")])
    conclusion = await engine.think(_snap(), [], BrainThinkEffort.MAX)
    assert len(backend.calls) == 2, f"MAX 档 8 轮上限，但消化完就该停: {len(backend.calls)}"
    assert conclusion.draft == "答案是博丽神社"


async def test_bad_json_round_keeps_tool_result_and_corrects():
    """消化工具结果的那一轮 JSON 坏了：下一轮仍带着工具结果 + 纠偏指令重来"""
    engine, backend = _engine(
        [_think(thought=f"查 {_TOOL_CALL}"), "{截断的输出", _think(hint="博丽神社")],
    )
    await engine.think(_snap(), [], BrainThinkEffort.MID)
    third = " ".join(m.content for m in backend.calls[2])
    assert "【工具执行结果】" in third and "博丽神社" in third, "坏一轮不该丢工具结果"
    assert "合法 JSON" in third, "应带纠偏指令"


async def test_bad_final_round_keeps_earlier_thinking():
    """最后一轮坏了：按此前的思考收尾，不整轮作废退回快速路径"""
    engine, _ = _engine([_think(cont=True, hint="先前想好的骨架"), "完全不是 JSON"])
    conclusion = await engine.think(_snap(), [], BrainThinkEffort.LOW)
    assert conclusion.draft == "先前想好的骨架"


async def test_unregistered_shout_is_not_a_tool_call():
    """「需要使用 JSON 格式回复」会被喊话解析成 JSON 工具：未注册的工具不执行、不续轮"""
    engine, backend = _engine([_think(thought="需要使用 JSON 格式回复")])
    await engine.think(_snap(), [], BrainThinkEffort.MAX)
    assert len(backend.calls) == 1
    assert known_calls([ToolCall(name="JSON"), ToolCall(name="lookup")], [_TOOL]) == [
        ToolCall(name="lookup")
    ]


async def test_ooc_prefilter_ignores_fetched_tool_text():
    """OOC 规则预筛只看模型自写部分：工具抓回的「语言模型」字样不该把查到的事实丢掉"""

    def _search() -> str:
        """搜索（测试工具）"""
        return "大语言模型是一种人工智能技术"

    tool = ToolSpec(tool_func=_search, desc="搜", name="search")
    backend = _Scripted([_think(cont=True), _think(hint="这么说吧", thought="查 调用 search {}")])
    sm = SessionManager()
    sm.set_default_backend(backend)
    engine = BrainEngine(sm, tools=[tool], ooc=OOCDetector(sm))
    conclusion = await engine.think(_snap("什么是大语言模型"), [], BrainThinkEffort.LOW)
    assert conclusion.ooc_flag is False
    assert conclusion.draft is not None and "人工智能技术" in conclusion.draft


async def test_pipeline_repeated_shout_does_not_hit_unreachable():
    """思考链步骤连喊两次工具：工具额度用完后按普通输出解析，不再撞 AssertionError"""
    shout = '{"thought": "调用 lookup {}", "note": "待定"}'
    backend = _Scripted([shout, shout, _CONCLUSION])
    sm = SessionManager()
    sm.set_default_backend(backend)
    step = ThinkStep(name="anchor", instructions="查", tools=True)
    conclusion = await ThinkPipeline("t", step).run(
        snapshot=_snap(),
        memories=[],
        sessions=sm,
        persona="p",
        effort=BrainThinkEffort.LOW,
        tools=[_TOOL],
    )
    assert conclusion.verdict == "draft"
    assert conclusion.reasoning_steps[0].thought == "待定"


async def test_pipeline_tool_round_still_gets_json_retry():
    """工具回合之后输出坏了：仍有一次纠偏重试（此前两者共用 range(2) 额度）"""
    shout = '{"thought": "调用 lookup {}", "note": "待定"}'
    backend = _Scripted([shout, "坏输出", '{"note": "查到了"}', _CONCLUSION])
    sm = SessionManager()
    sm.set_default_backend(backend)
    step = ThinkStep(name="anchor", instructions="查", tools=True)
    conclusion = await ThinkPipeline("t", step).run(
        snapshot=_snap(),
        memories=[],
        sessions=sm,
        persona="p",
        effort=BrainThinkEffort.LOW,
        tools=[_TOOL],
    )
    assert conclusion.reasoning_steps[0].thought == "查到了"
    retry = " ".join(m.content for m in backend.calls[2])
    assert "博丽神社" in retry, "纠偏重试应保留此前的工具回填"


async def test_pipeline_uses_configured_tool_result_limit():
    """思考链的工具执行沿用配置的结果长度上限（此前固定用默认值）"""
    shout = '{"thought": "调用 lookup {}", "note": "待定"}'
    backend = _Scripted([shout, '{"note": "ok"}', _CONCLUSION])
    sm = SessionManager()
    sm.set_default_backend(backend)
    step = ThinkStep(name="anchor", instructions="查", tools=True)
    await ThinkPipeline("t", step).run(
        snapshot=_snap(),
        memories=[],
        sessions=sm,
        persona="p",
        effort=BrainThinkEffort.LOW,
        tools=[_TOOL],
        tool_max_result_chars=12,
    )
    fed = backend.calls[1][-1].content
    assert "博丽神社维护" not in fed, f"结果应按配置截断: {fed}"


def test_think_step_owners_route_to_brain_backend():
    """brain.think.<步骤> / brain.think.conclusion 落到 brain.think 注册的模型"""
    default, brain = _Scripted([]), _Scripted([])
    sm = SessionManager()
    sm.set_default_backend(default)
    sm.register_backend("brain.think", brain)
    assert sm._get_backend("brain.think.emotion_check") is brain
    assert sm._get_backend("brain.think.conclusion") is brain
    assert sm._get_backend("gate.think") is default


def test_gate_fallback_ignores_own_utterances():
    """裁判故障的「点名才回」兜底：角色自己说过的话不算被点名"""
    snapshot = SceneSnapshot(sender="魔理沙", content="今天天气不错")
    decision = _fallback(snapshot, "幽幽子", ["幽幽子: 啊啦，客人来了呢"], "裁判异常")
    assert decision.reply is False
    named = _fallback(snapshot, "幽幽子", ["魔理沙: 幽幽子呢？"], "裁判异常")
    assert named.reply is True


def test_tool_registry_decorator_with_kwargs_and_name():
    """@ToolRegistry.tool(desc=..., name=...) 可用，name 进到 spec（模型看到的名字）"""
    saved = dict(ToolRegistry._tools)
    try:

        @ToolRegistry.tool(desc="检索", name="search_memory")
        def _impl(query: str) -> str:
            return query

        spec = ToolRegistry.get("search_memory")
        assert spec.tool_name == "search_memory"
        assert spec.desc == "检索"
    finally:
        ToolRegistry._tools.clear()
        ToolRegistry._tools.update(saved)

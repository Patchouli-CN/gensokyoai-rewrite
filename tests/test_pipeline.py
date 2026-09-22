"""思考链流水测试：顺序执行 / digest 交接 / 失败策略 / 超时 / 取消穿透 / DSL / 卡片驱动

异步红线（对应 CHANGES 0.0.19 的 fire-and-forget 教训）：
- run() 内步骤**串行 await**，无 asyncio.gather、无 create_task；
- 每步 wait_for 超时隔离；
- 只捕 Exception，asyncio.CancelledError 原样穿透；
- 全部调用 stateless，不落任何会话历史。
"""

import asyncio
import types

import pytest

from gensokyoai.core.brain.pipeline import PipelineAbort, ThinkPipeline, ThinkStep
from gensokyoai.core.session_manager import SessionManager
from gensokyoai.roleplay.character import Character, CharacterCard
from gensokyoai.roleplay.loop import TouhouWorld
from gensokyoai.schemas.brain_schema import BrainThinkEffort
from gensokyoai.schemas.model_schema import CompletionResult
from gensokyoai.schemas.scene_schema import SceneSnapshot

_SLEEP = "!sleep"
""" 让假后端睡死的暗号（测超时/取消） """

_CONCLUSION_DRAFT = '{"verdict": "draft", "intent": "调侃", "emotion": "愉悦", "draft": "啊啦～", "confidence": 0.8}'
_CONCLUSION_PASS = (
    '{"verdict": "pass_through", "intent": "倾听", "emotion": "平静", "confidence": 0.4}'
)


class _ScriptBackend:
    """按调用序号回话（顺序确定：各步骤依次 + 结论步最后）；记录每次调用"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list] = []
        self.kwargs: list[dict] = []

    async def chat(self, messages, *, max_new_tokens=512, temperature=0.7, stop=None, tools=None):
        self.calls.append(list(messages))
        self.kwargs.append({"max_new_tokens": max_new_tokens, "temperature": temperature})
        reply = self._replies.pop(0) if self._replies else '{"note": "兜底结论"}'
        if reply == _SLEEP:
            await asyncio.sleep(10)
        return CompletionResult(content=reply)

    def normalize_tool_calls(self, result, parsed_content=None):
        return result


def _sessions(replies: list[str]) -> tuple[SessionManager, _ScriptBackend]:
    backend = _ScriptBackend(replies)
    sessions = SessionManager()
    sessions.set_default_backend(backend)
    return sessions, backend


def _snapshot(text: str = "你觉得呢？") -> SceneSnapshot:
    return SceneSnapshot(
        scene_type="group_chat",
        sender="灵梦",
        content=text,
        context_snippet=["妖梦: 早上好", "灵梦: 幽幽子在吗"],
    )


_STEP_A = ThinkStep(
    name="emotion", instructions="评估情绪反应", max_tokens=120, temperature=0.3, timeout_s=5.0
)
_STEP_B = ThinkStep(name="relationship", instructions="扫描人际关系", timeout_s=5.0)


def _chain(*steps: ThinkStep | str) -> ThinkPipeline:
    return ThinkPipeline("测试链", *steps)


# ---------- 正常路径 ----------


async def test_runs_steps_in_order_with_digest_handoff():
    """串行执行；第二步看到第一步结论；每步无状态小调用；结论映射 BrainConclusion"""
    sessions, backend = _sessions(
        ['{"note": "被逗乐了"}', '{"note": "她把我当自己人"}', _CONCLUSION_DRAFT]
    )
    conclusion = await _chain(_STEP_A, _STEP_B).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="幽幽子人设",
        effort=BrainThinkEffort.MID,
    )

    assert len(backend.calls) == 3, "两步 + 结论步，一次不少"
    assert "评估情绪反应" in backend.calls[0][-1].content
    assert "（这是第一步）" in backend.calls[0][-1].content
    assert "扫描人际关系" in backend.calls[1][-1].content
    assert "被逗乐了" in backend.calls[1][-1].content, "digest 交接：第二步应看到第一步结论"
    assert backend.kwargs[0]["max_new_tokens"] == 120
    assert backend.kwargs[0]["temperature"] == 0.3

    assert sessions.usage("brain.think.emotion") == 0, "无状态调用不落会话历史"
    assert conclusion.verdict == "draft"
    assert conclusion.draft == "啊啦～"
    assert conclusion.intent == "调侃"
    assert conclusion.emotion == "愉悦"
    assert conclusion.effort is BrainThinkEffort.MID
    assert len(conclusion.reasoning_steps) == 2
    assert conclusion.reasoning_steps[0].thought == "被逗乐了"
    assert conclusion.reasoning_steps[0].round == 1
    assert conclusion.reasoning_steps[1].round == 2


async def test_conclusion_without_draft_is_pass_through():
    """结论无行动指令时 verdict=pass_through（与接力思考同口径）"""
    sessions, _ = _sessions(['{"note": "没什么想法"}', _CONCLUSION_PASS])
    conclusion = await _chain(_STEP_A).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.LOW,
    )
    assert conclusion.verdict == "pass_through"
    assert conclusion.draft is None


async def test_digest_handoff_is_capped():
    """digest 交接有长度上限，前几步结论无限堆积不会撑爆上下文"""
    long_note = "x" * 300
    sessions, backend = _sessions(
        [
            f'{{"note": "{long_note}A"}}',
            f'{{"note": "{long_note}B"}}',
            '{"note": "c"}',
            _CONCLUSION_PASS,
        ]
    )
    await _chain(_STEP_A, _STEP_B, ThinkStep("c", "第三步")).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.HIGH,
    )
    third_user = backend.calls[2][-1].content
    assert len(third_user) < 1200, f"digest 未收敛: {len(third_user)}"


# ---------- 失败策略 ----------


async def test_optional_step_failure_retries_once_then_skips():
    """可选步骤：解析失败纠正重试一次，仍败则跳过继续（韧性优先）"""
    sessions, backend = _sessions(["不是JSON", "还是不是JSON", _CONCLUSION_PASS])
    conclusion = await _chain(_STEP_A).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.LOW,
    )
    assert len(backend.calls) == 3, "两步尝试（含纠偏重试）+ 结论步"
    assert "不是合法 JSON" in backend.calls[1][-1].content, "第二次尝试应带纠偏指令"
    assert conclusion.verdict == "pass_through"
    assert conclusion.reasoning_steps == [], "失败的步骤不进轨迹"


async def test_required_step_failure_aborts_chain():
    """必要步骤失败：两次尝试后抛 PipelineAbort，且不跑结论步"""
    sessions, backend = _sessions(["不是JSON", "还是不是JSON", _CONCLUSION_PASS])
    required = ThinkStep(name="must", instructions="必须想出结果", optional=False)
    with pytest.raises(PipelineAbort, match="must"):
        await _chain(required).run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.LOW,
        )
    assert len(backend.calls) == 2, "中止后不应再调结论步"


async def test_conclusion_failure_raises_abort():
    """结论步也失败：抛 PipelineAbort（由调用方降级，不返回半成品）"""
    sessions, _ = _sessions(['{"note": "想过了"}', "结论不是JSON"])
    with pytest.raises(PipelineAbort, match="结论步骤失败"):
        await _chain(_STEP_A).run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.LOW,
        )


async def test_empty_pipeline_aborts():
    """空链直接中止，不浪费任何模型调用"""
    sessions, backend = _sessions([])
    with pytest.raises(PipelineAbort, match="空"):
        await ThinkPipeline("空链").run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.LOW,
        )
    assert backend.calls == []


# ---------- 超时与取消（异步红线） ----------


async def test_step_timeout_isolated_and_optional_skip():
    """慢步骤被 wait_for 掐断；按可选步骤跳过，链路继续"""
    sessions, backend = _sessions([_SLEEP, _CONCLUSION_PASS])
    slow = ThinkStep(name="slow", instructions="想太久", timeout_s=0.05)
    conclusion = await _chain(slow).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.LOW,
    )
    assert conclusion.verdict == "pass_through"
    assert len(backend.calls) == 2, "超时的步骤 + 结论步"


async def test_required_step_timeout_aborts():
    """必要步骤超时同样中止整链"""
    sessions, _ = _sessions([_SLEEP])
    slow = ThinkStep(name="slow", instructions="想太久", timeout_s=0.05, optional=False)
    with pytest.raises(PipelineAbort, match="slow"):
        await _chain(slow).run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.LOW,
        )


async def test_cancellation_propagates_through_run():
    """取消穿透：run() 中途被 cancel 时 CancelledError 原样抛出，不被吞"""
    sessions, _ = _sessions([_SLEEP])
    task = asyncio.create_task(
        _chain(_STEP_A).run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.LOW,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


# ---------- 档位动态裁深 ----------

_STEP_C = ThinkStep(name="memory", instructions="勾连记忆")


async def test_effort_low_runs_last_step_only():
    """LOW：只留收尾步（日常寒暄不铺垫，1 次步骤调用）"""
    sessions, backend = _sessions(['{"note": "A"}', '{"note": "B"}', _CONCLUSION_DRAFT])
    conclusion = await _chain(_STEP_A, _STEP_B).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.LOW,
    )
    assert len(backend.calls) == 2, "LOW 只跑收尾步 + 结论"
    assert "扫描人际关系" in backend.calls[0][-1].content, "收尾步是链尾"
    assert len(conclusion.reasoning_steps) == 1


async def test_effort_mid_runs_first_and_last():
    """MID：首 + 尾（跳过中间铺垫，2 次步骤调用）"""
    sessions, backend = _sessions(['{"note": "a"}', '{"note": "c"}', _CONCLUSION_PASS])
    conclusion = await _chain(_STEP_A, _STEP_B, _STEP_C).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.MID,
    )
    assert len(backend.calls) == 3, "MID = 首 + 尾 + 结论"
    assert "评估情绪反应" in backend.calls[0][-1].content
    assert "勾连记忆" in backend.calls[1][-1].content, "尾步收口"
    assert len(conclusion.reasoning_steps) == 2


async def test_effort_high_runs_full_chain():
    """HIGH：全链"""
    sessions, backend = _sessions(
        ['{"note": "a"}', '{"note": "b"}', '{"note": "c"}', _CONCLUSION_PASS]
    )
    conclusion = await _chain(_STEP_A, _STEP_B, _STEP_C).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.HIGH,
    )
    assert len(backend.calls) == 4, "HIGH = 全链 + 结论"
    assert len(conclusion.reasoning_steps) == 3


async def test_effort_max_deep_mode_doubles_budget_and_cools():
    """MAX：全链 + 每步深思考（预算 ×2、温度降到 0.2）"""
    sessions, backend = _sessions(['{"note": "a"}', '{"note": "b"}', _CONCLUSION_DRAFT])
    await _chain(_STEP_A, _STEP_B).run(
        snapshot=_snapshot(),
        memories=[],
        sessions=sessions,
        persona="p",
        effort=BrainThinkEffort.MAX,
    )
    assert backend.kwargs[0]["max_new_tokens"] == _STEP_A.max_tokens * 2
    assert backend.kwargs[0]["temperature"] == 0.2
    assert backend.kwargs[-1]["max_new_tokens"] == 600, "结论步也翻倍（300×2）"
    assert backend.kwargs[-1]["temperature"] == 0.2


async def test_effort_none_aborts_without_calling():
    """NONE 不跑链（调用方应走快速路径；跑到这里是防御性中止，零调用）"""
    sessions, backend = _sessions([])
    with pytest.raises(PipelineAbort, match="NONE"):
        await _chain(_STEP_A).run(
            snapshot=_snapshot(),
            memories=[],
            sessions=sessions,
            persona="p",
            effort=BrainThinkEffort.NONE,
        )
    assert backend.calls == []


# ---------- DSL / 卡片驱动 ----------


def test_rshift_accepts_string_and_merges_chains():
    """裸字符串收编为步骤；链合并以左链标签为准"""
    chain = ThinkPipeline("l") >> "先感受气氛" >> ThinkStep("s2", "再扫描关系")
    assert chain.items[0].name == "step1"
    assert chain.items[0].instructions == "先感受气氛"
    assert chain.items[1].name == "s2"

    merged = ThinkPipeline("a", ThinkStep("x", "X")) >> ThinkPipeline("b", ThinkStep("y", "Y"))
    assert [step.name for step in merged] == ["x", "y"]
    assert merged.label == "a"


def test_from_names_builds_steps_from_prompt_registry():
    """卡片驱动：步骤名 -> think.<名> 注册提示词"""
    chain = ThinkPipeline.from_names("卡片链", ["emotion_check", "stance_decide"])
    assert [step.name for step in chain] == ["emotion_check", "stance_decide"]
    assert "情绪" in chain.items[0].instructions


def test_from_names_unknown_step_raises():
    """未注册的步骤名启动即暴露（卡片配置错误不拖到运行时）"""
    with pytest.raises(KeyError):
        ThinkPipeline.from_names("坏链", ["不存在的步骤"])


# ---------- 世界接线 ----------


def _make_world(tmp_path, think_chain: list[str]) -> TouhouWorld:
    sessions = SessionManager()
    sessions.set_default_backend(_ScriptBackend([]))
    character = Character(
        CharacterCard(name="幽幽子", system_prompt="白玉楼的主人", think_chain=think_chain)
    )
    return TouhouWorld(
        eye=types.SimpleNamespace(_stop_requested=True),
        character=character,
        sessions=sessions,
        storage_dir=tmp_path,
    )


async def test_world_builds_pipeline_from_card(tmp_path):
    """角色卡配了 think_chain -> BrainEngine 带上思考链"""
    world = _make_world(tmp_path, ["emotion_check", "stance_decide"])
    pipeline = world.brain._pipeline
    assert pipeline is not None
    assert [step.name for step in pipeline] == ["emotion_check", "stance_decide"]
    assert pipeline.label == "幽幽子·思考链"


async def test_world_without_think_chain_uses_relay(tmp_path):
    """卡片没配链 -> pipeline=None，维持内置接力思考"""
    world = _make_world(tmp_path, [])
    assert world.brain._pipeline is None


async def test_world_persona_brief_for_thinking(tmp_path):
    """「想方向」的环节用人设摘要（压 token）；完整人设只留给 Responder"""
    sessions = SessionManager()
    sessions.set_default_backend(_ScriptBackend([]))
    character = Character(
        CharacterCard(name="幽幽子", system_prompt="设定" * 500, think_chain=["emotion_check"])
    )
    world = TouhouWorld(
        eye=types.SimpleNamespace(_stop_requested=True),
        character=character,
        sessions=sessions,
        storage_dir=tmp_path,
    )
    assert world._persona_brief.startswith("【幽幽子】")
    assert len(world._persona_brief) < 260, "摘要截断，不把整本人设塞进每一步思考"
    assert world.brain._persona_brief == world._persona_brief
    assert world.responder._persona == character.prompt, "Responder 用完整人设"

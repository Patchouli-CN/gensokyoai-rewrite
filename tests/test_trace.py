"""思考轨迹留档测试：JSONL 写入 / 逐轮步骤 / 轮转 / 开关 / 失败隔离"""

import json

from gensokyoai.roleplay.trace import ReasoningTrace
from gensokyoai.schemas.brain_schema import BrainConclusion, ReasoningStep


def _conclusion() -> BrainConclusion:
    """带逐轮步骤与两种 reasoning 的结论"""
    return BrainConclusion(
        verdict="draft",
        intent="追问",
        emotion="好奇",
        draft="顺着问下去",
        confidence=0.8,
        _reasoning="工程实现思考",
        _raw_reasoning="模型原生思考",
        reasoning_steps=[
            ReasoningStep(round=1, thought="先想想", need_continue_think=True),
            ReasoningStep(
                round=2, thought="想好了", need_continue_think=False, action_hint="问回去"
            ),
        ],
    )


async def test_record_writes_jsonl_with_steps(tmp_path):
    """一回合一行，含逐轮 ReasoningStep 与两种 reasoning"""
    trace = ReasoningTrace(tmp_path, "s1")
    await trace.record(turn=1, effort="high", conclusion=_conclusion(), reply="嗯？")

    lines = trace.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])

    assert entry["turn"] == 1
    assert entry["effort"] == "high"
    assert entry["intent"] == "追问"
    assert entry["draft"] == "顺着问下去"
    assert entry["reasoning"] == "工程实现思考", "工程实现那份"
    assert entry["raw_reasoning"] == "模型原生思考", "模型原生那份"
    assert [s["round"] for s in entry["steps"]] == [1, 2]
    assert entry["steps"][0]["need_continue_think"] is True
    assert entry["steps"][1]["action_hint"] == "问回去"
    assert entry["reply"] == "嗯？"


async def test_record_appends_one_line_per_turn(tmp_path):
    """追加写：每回合一行，顺序保持"""
    trace = ReasoningTrace(tmp_path, "s1")
    for turn in (1, 2, 3):
        await trace.record(turn=turn, effort="low", conclusion=_conclusion(), reply=f"回复{turn}")

    lines = trace.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert [json.loads(x)["turn"] for x in lines] == [1, 2, 3]


async def test_rotation_keeps_previous_generation(tmp_path):
    """超出大小上限时轮转，保留上一代（.1）"""
    trace = ReasoningTrace(tmp_path, "s1", max_bytes=300)
    for turn in range(1, 6):
        await trace.record(turn=turn, effort="low", conclusion=_conclusion(), reply="x" * 50)

    assert trace.path.exists()
    assert trace.path.with_suffix(".jsonl.1").exists(), "超限应保留上一代"


async def test_disabled_trace_writes_nothing(tmp_path):
    """关掉留档时一个字节都不写"""
    trace = ReasoningTrace(tmp_path, "s1", enabled=False)
    assert trace.enabled is False

    await trace.record(turn=1, effort="low", conclusion=_conclusion())
    assert not trace.path.exists()


async def test_trace_failure_is_isolated(tmp_path):
    """写盘失败只记日志，不抛给主链路"""
    trace = ReasoningTrace(tmp_path, "s1")
    trace.path.parent.mkdir(parents=True, exist_ok=True)
    trace.path.mkdir()  # 让目标路径变成目录 -> 追加必然失败

    await trace.record(turn=1, effort="low", conclusion=_conclusion())
    assert trace.path.is_dir(), "失败被吞掉，未影响调用方"


async def test_trace_sessions_are_isolated(tmp_path):
    """不同会话写各自的文件"""
    first = ReasoningTrace(tmp_path, "room-a")
    second = ReasoningTrace(tmp_path, "room-b")
    await first.record(turn=1, effort="low", conclusion=_conclusion())
    await second.record(turn=1, effort="low", conclusion=_conclusion())

    assert first.path != second.path
    assert first.path.exists() and second.path.exists()

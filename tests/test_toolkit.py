"""工具执行器单元测试：截断 / 超时 / 同步下线程 / 结构化错误 / 命名统一 / 内置工具"""

import asyncio
import time

from gensokyoai.core.registry import ToolRegistry
from gensokyoai.core.toolkit import ToolExecutor, build_executor
from gensokyoai.schemas.model_schema import ToolCall, ToolSpec
from gensokyoai.tools import get_current_dateinfo, get_current_time, get_moon_phase


def echo(text: str) -> str:
    """回显"""
    return text


async def aadd(a: int, b: int) -> str:
    """异步相加"""
    await asyncio.sleep(0)
    return str(a + b)


def big(n: int) -> str:
    """返回超长文本"""
    return "x" * n


def boom() -> str:
    """总是抛异常"""
    raise RuntimeError("炸了")


def slow() -> str:
    """慢工具（同步阻塞）"""
    time.sleep(0.5)
    return "slow"


async def test_execute_success_sync_and_async():
    """同步与异步工具都能正确执行"""
    executor = ToolExecutor([ToolSpec(echo), ToolSpec(aadd)])
    sync_result = await executor.execute("echo", '{"text": "hi"}')
    async_result = await executor.execute("aadd", {"a": 1, "b": 2})
    assert sync_result.ok and sync_result.content == "hi"
    assert async_result.ok and async_result.content == "3"


async def test_unknown_tool_returns_structured_error():
    """未知工具返回结构化错误，而非抛异常"""
    result = await ToolExecutor([]).execute("nope")
    assert not result.ok
    assert "未知工具" in result.error
    assert "未知工具" in result.to_model_text()


async def test_bad_json_arguments_rejected():
    """参数不是合法 JSON 时明确拒绝（旧实现会静默当空参执行）"""
    result = await ToolExecutor([ToolSpec(echo)]).execute("echo", "这不是JSON")
    assert not result.ok
    assert "JSON" in result.error


async def test_exception_is_caught_and_classified():
    """工具抛异常被兜住，错误里带类型与原文"""
    result = await ToolExecutor([ToolSpec(boom)]).execute("boom")
    assert not result.ok
    assert "RuntimeError" in result.error
    assert "炸了" in result.error


async def test_result_truncated_to_limit():
    """超长结果被截断并标注（保护上下文窗口）"""
    result = await ToolExecutor([ToolSpec(big)], max_result_chars=50).execute("big", '{"n": 500}')
    assert result.ok
    assert len(result.content) <= 50
    assert "截断" in result.content


async def test_timeout_returns_error():
    """超时的工具返回超时错误，不挂住调用链"""
    result = await ToolExecutor([ToolSpec(slow)], timeout=0.05).execute("slow")
    assert not result.ok
    assert "超时" in result.error


async def test_sync_tool_runs_off_event_loop():
    """同步工具下线程执行：执行期间事件循环仍能推进"""
    executor = ToolExecutor([ToolSpec(slow)], timeout=2.0)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    result = await executor.execute("slow")
    task.cancel()

    assert result.ok
    assert ticks > 0, "同步工具不应阻塞事件循环"


async def test_execute_many_pairs_one_to_one():
    """批量执行结果与入参一一对应，单个失败不影响其他"""
    executor = ToolExecutor([ToolSpec(echo), ToolSpec(boom)])
    calls = [
        ToolCall(id="1", name="echo", arguments='{"text": "a"}'),
        ToolCall(id="2", name="boom", arguments="{}"),
    ]
    results = await executor.execute_many(calls)
    assert [r.name for r in results] == ["echo", "boom"]
    assert results[0].ok and not results[1].ok


def test_tool_name_override_is_used_everywhere():
    """显式 name 覆盖：tool_name 与 OpenAI 声明都用它（原先只用函数名）"""
    spec = ToolSpec(echo, name="say")
    assert spec.tool_name == "say"
    assert spec.to_openai_tool()["function"]["name"] == "say"
    assert "say" in spec.prompt()
    assert ToolExecutor([spec]).names == ["say"]


def test_builtin_tools_are_registered():
    """内置工具经装饰器注册进全局表（bootstrap 扫描后启动即可取）"""
    names = {tool.tool_name for tool in ToolRegistry.all()}
    assert {"get_current_time", "get_current_dateinfo", "get_moon_phase"} <= names


async def test_builtin_tools_are_executable():
    """内置工具能通过执行器实际调用"""
    executor = build_executor(
        [ToolSpec(get_current_time), ToolSpec(get_current_dateinfo), ToolSpec(get_moon_phase)]
    )
    time_result = await executor.execute("get_current_time")
    date_result = await executor.execute("get_current_dateinfo")
    moon_result = await executor.execute("get_moon_phase")

    assert time_result.ok and "-" in time_result.content
    assert date_result.ok and "星期" in date_result.content
    assert moon_result.ok and "月" in moon_result.content

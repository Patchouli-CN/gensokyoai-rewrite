"""后台任务管理器：强引用 / 异常记录 / 收尾。

`asyncio` 的事件循环对任务只持**弱引用**，`create_task()` 的返回值没人接，
任务就可能执行途中被 GC 回收（既不报错也没有堆栈）。真实回收时机难以稳定复现，
所以这里断言的是不变式：管理器自己持引用、异常有人认领、关闭时能收尾。
"""

import asyncio
import gc

import pytest
from loguru import logger

from gensokyoai.utils.tasks import TaskManager


class _Sink:
    """收集 loguru 记录，便于断言异常是否被记录。"""

    def __init__(self) -> None:
        self.records: list[str] = []

    def __call__(self, message) -> None:
        self.records.append(message.record["message"])


@pytest.fixture
def error_sink():
    """临时挂一个只收 ERROR 及以上的 sink。"""
    sink = _Sink()
    handler_id = logger.add(sink, level="ERROR")
    yield sink
    logger.remove(handler_id)


async def test_manager_holds_reference_without_return_handle():
    """不接 spawn 的返回值也照样跑完：管理器持强引用，结束后自动摘除"""
    manager = TaskManager("TEST")
    done = asyncio.Event()

    async def worker() -> None:
        for _ in range(5):
            await asyncio.sleep(0.01)
        done.set()

    manager.spawn(worker(), name="worker")
    gc.collect()  # 主动触发回收：若没人持引用，任务可能就此消失
    assert manager.pending == 1, "在途任务应被管理器持有"

    await asyncio.wait_for(done.wait(), timeout=1.0)
    await asyncio.sleep(0.01)  # 任务结束到 done 回调执行之间还隔一次事件循环
    assert manager.pending == 0, "结束后应摘除引用，避免集合无限增长"


async def test_task_exception_is_logged(error_sink):
    """后台任务异常统一记录，不退化成 'Task exception was never retrieved'"""
    manager = TaskManager("TEST")

    async def boom() -> None:
        raise ValueError("侧链炸了")

    manager.spawn(boom(), name="boom")
    await asyncio.sleep(0.05)

    assert manager.pending == 0
    assert any("后台任务失败: boom" in m for m in error_sink.records)


async def test_cancelled_task_is_not_logged_as_error(error_sink):
    """取消是预期行为，不该记 ERROR"""
    manager = TaskManager("TEST")

    async def sleeper() -> None:
        await asyncio.sleep(10)

    manager.spawn(sleeper(), name="sleeper")
    await asyncio.sleep(0)
    assert await manager.cancel_all() == 1
    await asyncio.sleep(0.05)

    assert error_sink.records == []
    assert manager.pending == 0


async def test_drain_waits_and_covers_nested_spawn():
    """drain 等到在途任务结束，并连带覆盖任务链上新 spawn 的任务"""
    manager = TaskManager("TEST")
    inner_done = asyncio.Event()

    async def inner() -> None:
        await asyncio.sleep(0.05)
        inner_done.set()

    async def outer() -> None:
        await asyncio.sleep(0.02)
        manager.spawn(inner(), name="inner")

    manager.spawn(outer(), name="outer")
    finished = await manager.drain(timeout=2.0)

    assert inner_done.is_set(), "drain 必须连带等到任务链上新 spawn 的任务"
    assert finished == 2, "outer 与它派生的 inner 都应被等到"
    assert manager.pending == 0


async def test_drain_timeout_cancels_remaining():
    """超时未结束的任务被取消，且 finally 正常执行（不是被硬掐）"""
    manager = TaskManager("TEST")
    cleaned = False

    async def slow() -> None:
        nonlocal cleaned
        try:
            await asyncio.sleep(10)
        finally:
            cleaned = True

    manager.spawn(slow(), name="slow")
    await asyncio.sleep(0.01)
    await manager.drain(timeout=0.05)

    assert cleaned, "取消应让 finally 跑到"
    assert manager.pending == 0


def test_spawn_without_running_loop_raises():
    """没有事件循环时直接报错（与 asyncio.create_task 行为一致，不静默吞掉）"""
    manager = TaskManager("TEST")

    async def noop() -> None:
        return None

    coro = noop()
    with pytest.raises(RuntimeError):
        manager.spawn(coro)
    coro.close()  # 避免 "coroutine was never awaited" 告警

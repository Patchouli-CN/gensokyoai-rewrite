"""生物钟测试：周期任务 / 一次性任务 / 异常隔离 / 注销 / 优雅停止"""

import asyncio

import pytest

from gensokyoai.core.clock import BiologicalClock


async def test_every_fires_repeatedly():
    """周期任务按间隔重复触发"""
    clock = BiologicalClock()
    hits = []
    clock.every("heartbeat", 0.02, lambda: hits.append(1) or _noop())
    await clock.start()
    await asyncio.sleep(0.07)
    await clock.stop()
    assert len(hits) >= 2


async def test_once_fires_exactly_once():
    """一次性任务只触发一次，之后自动注销"""
    clock = BiologicalClock()
    hits = []
    clock.once("reminder", 0.02, lambda: hits.append(1) or _noop())
    await clock.start()
    await asyncio.sleep(0.06)
    assert len(hits) == 1
    assert "reminder" not in clock.names  # 执行完自动移除
    await clock.stop()


async def test_job_exception_does_not_kill_clock():
    """任务炸了就进日志，其他任务与心跳不受影响"""
    clock = BiologicalClock()
    good_hits = []

    async def _boom():
        raise RuntimeError("炸了")

    clock.every("bad", 0.02, _boom)
    clock.every("good", 0.02, lambda: good_hits.append(1) or _noop())
    await clock.start()
    await asyncio.sleep(0.07)
    await clock.stop()
    assert len(good_hits) >= 2


async def test_cancel_removes_job():
    """注销：未启动的任务不再触发，运行中的任务被取消"""
    clock = BiologicalClock()
    hits = []
    clock.every("temp", 0.02, lambda: hits.append(1) or _noop())
    assert clock.cancel("temp") is True
    assert clock.cancel("不存在") is False
    await clock.start()
    await asyncio.sleep(0.05)
    await clock.stop()
    assert hits == []


async def test_stop_cancels_running_jobs():
    """stop 后任务不再触发（世界关了钟就停）"""
    clock = BiologicalClock()
    hits = []
    clock.every("heartbeat", 0.02, lambda: hits.append(1) or _noop())
    await clock.start()
    await asyncio.sleep(0.03)
    await clock.stop()
    count_at_stop = len(hits)
    await asyncio.sleep(0.05)
    assert len(hits) == count_at_stop


async def test_duplicate_registration_rejected():
    """同名重复注册报错（防静默覆盖）"""
    clock = BiologicalClock()
    clock.every("x", 1.0, _noop)
    with pytest.raises(ValueError):
        clock.every("x", 1.0, _noop)


async def _noop() -> None:
    pass

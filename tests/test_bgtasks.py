"""后台任务与落盘测试 —— bgtasks 监督 / roomlog 追加、轮转与回放。"""

import asyncio
import time

from gensokyoai.bgtasks import BgTasks
from gensokyoai.health import HealthCenter
from gensokyoai.room import Event, Room, RoomLog


async def _noop() -> None:
    pass


# ---------- bgtasks ----------


async def test_spawn_failure_counted_not_propagated():
    """一次性任务失败：记账进 health，登记处照常运转"""
    health = HealthCenter()
    tasks = BgTasks(health)

    async def boom() -> None:
        raise ValueError("炸")

    tasks.spawn("boom", boom())
    await tasks.idle()
    assert "bgtasks.boom.errors=1" in health.report()


async def test_every_runs_periodically_and_survives_failure():
    """周期任务按节奏跑；某轮失败下轮照常"""
    health = HealthCenter()
    tasks = BgTasks(health)
    calls = {"n": 0}

    async def tick() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("首轮失败")

    tasks.every("tick", 0.01, tick)
    deadline = time.monotonic() + 3.0
    while calls["n"] < 2 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await tasks.shutdown()
    assert calls["n"] >= 2, "失败后仍在跑"
    assert "bgtasks.tick.errors=1" in health.report()


async def test_shutdown_cancels_everything():
    """shutdown 取消全部任务，可重复调用"""
    tasks = BgTasks()
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    tasks.spawn("forever", forever())
    await started.wait()
    await tasks.shutdown(drain=0.01)
    await tasks.shutdown()
    assert not tasks._tasks


async def test_idle_does_not_wait_for_periodic_tasks():
    """idle 只等一次性任务，有周期任务在跑也不能挂住"""
    tasks = BgTasks()
    tasks.every("tick", 3600, _noop)
    await asyncio.wait_for(tasks.idle(), timeout=1.0)
    await tasks.shutdown()


async def test_shutdown_drains_pending_writes(tmp_path):
    """退出前写完排队的一次性任务，已入房的事件不丢"""
    tasks = BgTasks()
    room = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))
    for i in range(20):
        room.append("message", "甲", f"第{i}条")

    await tasks.shutdown()

    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20


async def test_shutdown_closes_log_handle(tmp_path):
    """退出时关闭文件句柄"""
    tasks = BgTasks()
    log = RoomLog(tmp_path / "events.jsonl", tasks)
    Room("g1", log=log).append("message", "甲", "你好")
    await tasks.idle()
    assert log._storage._file is not None and not log._storage._file.closed

    await tasks.shutdown()

    assert log._storage._file is None


# ---------- roomlog ----------


async def test_roomlog_write_and_replay(tmp_path):
    """note 落盘一行；load 回放全部"""
    tasks = BgTasks()
    log = RoomLog(tmp_path / "events.jsonl", tasks)
    await log.note(Event(kind="message", sender="你", text="你好"))
    await log.note(Event(kind="reply", sender="觉", text="你好呀"))

    events = log.load()

    assert [(e.kind, e.sender, e.text) for e in events] == [
        ("message", "你", "你好"),
        ("reply", "觉", "你好呀"),
    ]
    await tasks.shutdown()


async def test_roomlog_skips_broken_lines(tmp_path):
    """坏行跳过，好行照常回放"""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"kind":"message","sender":"你","text":"好","ts":1.0}\n烂行\n', "utf-8"
    )
    events = RoomLog(path, BgTasks()).load()
    assert len(events) == 1 and events[0].text == "好"


async def test_roomlog_task_name_is_per_room(tmp_path):
    """写盘任务名带房间名，health 计数不跨房间撞车"""
    tasks = BgTasks()
    first = RoomLog(tmp_path / "g1" / "events.jsonl", tasks)
    second = RoomLog(tmp_path / "g2" / "events.jsonl", tasks)

    task1 = first.note(Event(kind="message", sender="甲", text="x"))
    task2 = second.note(Event(kind="message", sender="甲", text="y"))

    assert (task1.get_name(), task2.get_name()) == ("roomlog:g1", "roomlog:g2")
    await tasks.idle()
    await tasks.shutdown()


async def test_roomlog_rotates_at_size_limit(tmp_path):
    """主文件超过上界就轮转到 .1"""
    path = tmp_path / "events.jsonl"
    tasks = BgTasks()
    log = RoomLog(path, tasks, max_bytes=1024)
    for i in range(200):
        await log.note(Event(kind="message", sender="甲", text=f"第{i}条" + "x" * 20))
    await tasks.idle()

    assert path.with_name(path.name + ".1").exists()
    assert path.stat().st_size <= 1024
    await tasks.shutdown()


async def test_roomlog_load_reads_tail_only(tmp_path):
    """回放只读末尾一段，长历史不拖住建房"""
    path = tmp_path / "events.jsonl"
    tasks = BgTasks()
    log = RoomLog(path, tasks, max_bytes=2048)
    for i in range(120):
        await log.note(Event(kind="message", sender="甲", text=f"第{i}条" + "y" * 20))
    await tasks.idle()

    events = log.load()

    assert events and len(events) < 120
    assert events[-1].text.startswith("第119条")
    await tasks.shutdown()


async def test_room_persists_and_replays(tmp_path):
    """房间挂 log：消息与发言都落盘，重开房间回放历史"""
    tasks = BgTasks()
    room = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))
    room.append("message", "你", "在吗")
    room.append("reply", "觉", "在")
    await tasks.idle()

    reloaded = Room("g1", log=RoomLog(tmp_path / "events.jsonl", tasks))

    assert [(e.kind, e.text) for e in reloaded.events] == [
        ("message", "在吗"),
        ("reply", "在"),
    ]
    await tasks.shutdown()

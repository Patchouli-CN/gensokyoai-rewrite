"""Storage 的行为（队列版）：提交即返回、worker 落盘、空闲刷、防饿死、轮转。

注意：ayafileio 的 write 是写穿的（另一个句柄立刻能读到），断言看的是
"有没有真的 flush"（计数与 dirty），不是"文件里有没有内容"。
"""

import asyncio

import pytest

from gensokyoai.bgtasks import BgTasks
from gensokyoai.storage import Storage


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Counting(Storage):
    """数一数真正落盘的次数。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.flushes = 0

    async def flush(self) -> None:
        if self._dirty:
            self.flushes += 1
        await super().flush()


async def test_write_is_ordered(tmp_path):
    """并发提交要保序"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=0)
    futures = [storage.write(f"{i}\n") for i in range(50)]
    await asyncio.gather(*(f for f in futures if f is not None))
    await storage.sync()
    await storage.aclose()

    lines = (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines == [str(i) for i in range(50)]


async def test_write_returns_immediately_then_worker_writes(tmp_path):
    """提交不阻塞：write() 返回后 worker 异步写"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=0)
    future = storage.write("hello\n")
    assert future is not None

    await future

    assert (tmp_path / "e.jsonl").read_text(encoding="utf-8") == "hello\n"
    await storage.aclose()


async def test_sync_drains_queue(tmp_path):
    """sync() 之后队列里的东西都写下去了"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=10_000)
    for index in range(10):
        storage.write(f"{index}\n")

    await storage.sync()

    lines = (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10
    await storage.aclose()


async def test_idle_flushes_after_quiet(tmp_path):
    """队列安静一会儿就自动落盘"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=50)
    storage.write("hello\n")

    for _ in range(80):
        if storage.flushes >= 1 and storage.dirty is False:
            break
        await asyncio.sleep(0.01)

    assert storage.flushes >= 1
    await storage.aclose()


async def test_busy_writes_are_not_flushed_per_line(tmp_path):
    """连续写不逐条 flush（FlushFileBuffers 几十毫秒起）"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=10_000)
    for index in range(20):
        storage.write(f"{index}\n")
    await storage.sync()

    assert storage.flushes == 0
    assert storage.dirty is True
    await storage.aclose()


async def test_dirty_cap_forces_flush_during_busy(tmp_path):
    """一直有人写也要按 max_dirty_s 兜底"""
    clock = _Clock()
    storage = _Counting(
        tmp_path / "e.jsonl", idle_ms=10_000, max_dirty_s=5.0, clock=clock
    )

    storage.write("a\n")
    await storage.sync()
    assert storage.flushes == 0, "还没到上限"

    clock.now += 6
    storage.write("b\n")
    await storage.sync()

    assert storage.flushes >= 1
    await storage.aclose()


async def test_close_flushes_pending(tmp_path):
    """关之前把没刷的刷掉"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=10_000)
    storage.write("last\n")
    await storage.sync()
    await storage.aclose()

    assert storage.flushes >= 1


async def test_rotation_keeps_latest_lines(tmp_path):
    """超过上限轮转成 .1，最新的行一定在、不跳号"""
    path = tmp_path / "e.jsonl"
    storage = Storage(path, idle_ms=0, max_bytes=1024)
    for index in range(80):
        storage.write(f"line-{index:04d}-" + "x" * 20 + "\n")
    await storage.sync()
    await storage.aclose()

    assert path.with_name(path.name + ".1").exists()
    current = path.read_text(encoding="utf-8").splitlines()
    assert current[-1].startswith("line-0079")
    numbers = [int(line.split("-")[1]) for line in current]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers)))


async def test_append_after_close_raises(tmp_path):
    """关掉之后再写要报错"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=0)
    await storage.aclose()

    with pytest.raises(RuntimeError):
        storage.write("x\n")


async def test_worker_registered_in_bgtasks(tmp_path):
    """worker 由 BgTasks 管，idle() 会把队列排空"""
    tasks = BgTasks()
    storage = Storage(tmp_path / "e.jsonl", tasks=tasks, idle_ms=10_000)
    storage.write("a\n")

    await tasks.idle()

    assert (tmp_path / "e.jsonl").read_text(encoding="utf-8") == "a\n"
    assert storage._worker is not None
    await tasks.shutdown()

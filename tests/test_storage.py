"""Storage 的行为：保序、空闲落盘、防饿死、轮转、关闭收尾。

注意：ayafileio 的 write() 是写穿的（另一个句柄立刻能读到），所以断言看的是
"有没有真的 flush"（dirty 状态 / 调用计数），不是"文件里有没有内容"。
"""

import asyncio

import pytest

from gensokyoai.storage import Storage


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Counting(Storage):
    """数一数真正落盘的次数（含 aclose 里那次）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.flushes = 0

    async def _flush_locked(self) -> None:
        if self._dirty:
            self.flushes += 1
        await super()._flush_locked()


async def test_append_writes_in_order(tmp_path):
    """并发追加要保序"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=0)
    await asyncio.gather(*(storage.append(f"{i}\n") for i in range(50)))
    await storage.aclose()

    lines = (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines == [str(i) for i in range(50)], "写入必须按调用顺序落盘"


async def test_write_is_visible_before_flush(tmp_path):
    """写穿：不 flush 别的句柄也能读到（所以进程被杀不会丢）"""
    path = tmp_path / "e.jsonl"
    storage = Storage(path, idle_ms=10_000)
    await storage.append("hello\n")

    assert path.read_text(encoding="utf-8") == "hello\n"
    assert storage.dirty is True, "内容可见但还没落盘"
    await storage.aclose()


async def test_idle_flushes_after_quiet(tmp_path):
    """安静一会儿就自动 flush"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=50)
    await storage.append("hello\n")

    for _ in range(40):  # 最多等 0.4 秒，别跟调度较劲
        if storage.dirty is False:
            break
        await asyncio.sleep(0.01)

    assert storage.flushes >= 1, "安静后应该刷过"
    assert storage.dirty is False
    await storage.aclose()


async def test_busy_writes_are_not_flushed_per_line(tmp_path):
    """连续写不逐条 flush（flush 是 FlushFileBuffers，几十毫秒起）"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=10_000)
    for index in range(20):
        await storage.append(f"{index}\n")

    assert storage.flushes == 0
    assert storage.dirty is True
    await storage.aclose()


async def test_dirty_cap_forces_flush_during_busy(tmp_path):
    """一直有人写也要按 max_dirty_s 兜底"""
    clock = _Clock()
    storage = _Counting(
        tmp_path / "e.jsonl", idle_ms=10_000, max_dirty_s=5.0, clock=clock
    )

    await storage.append("a\n")
    clock.now += 3
    await storage.append("b\n")
    assert storage.flushes == 0, "还没到上限"

    clock.now += 3  # 累计 6 秒 > 5
    await storage.append("c\n")

    assert storage.flushes == 1
    assert storage.dirty is False
    await storage.aclose()


async def test_close_flushes_pending(tmp_path):
    """关之前把脏数据刷掉"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=10_000)
    await storage.append("last\n")
    await storage.aclose()

    assert storage.flushes >= 1


async def test_rotation_keeps_content_contiguous(tmp_path):
    """超过上限轮转成 .1，最新的行一定在，不重复不跳号"""
    path = tmp_path / "e.jsonl"
    storage = Storage(path, idle_ms=0, max_bytes=1024)

    for index in range(80):
        await storage.append(f"line-{index:04d}-" + "x" * 20 + "\n")
    await storage.aclose()

    backup = path.with_name(path.name + ".1")
    assert backup.exists(), "应该有轮转备份"
    current = path.read_text(encoding="utf-8").splitlines()
    assert current[-1].startswith("line-0079"), "最新的行必须还在"
    numbers = [int(line.split("-")[1]) for line in current]
    assert numbers == list(range(numbers[0], numbers[0] + len(numbers))), "不该跳号"


async def test_append_after_close_raises(tmp_path):
    """关掉之后再写要报错，而不是静默吞掉"""
    storage = Storage(tmp_path / "e.jsonl", idle_ms=0)
    await storage.aclose()

    with pytest.raises(RuntimeError):
        await storage.append("x\n")


async def test_no_auto_flush_when_disabled(tmp_path):
    """idle_ms=0 时不自作主张刷"""
    storage = _Counting(tmp_path / "e.jsonl", idle_ms=0)
    await storage.append("a\n")

    await asyncio.sleep(0.1)

    assert storage.flushes == 0
    assert storage.dirty is True
    await storage.aclose()
    assert storage.flushes == 1

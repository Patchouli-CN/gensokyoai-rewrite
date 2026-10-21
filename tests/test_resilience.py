"""韧性测试 —— 任务自愈、能力降级、记忆容错。"""

import asyncio
import json

import pytest

from gensokyoai.hub import Hub
from gensokyoai.loop import Loop
from gensokyoai.memory import Memory
from gensokyoai.model import Reply, ToolCall
from gensokyoai.mouth import BroadcastMouth, ConsoleMouth, Mouth, WsMouth
from gensokyoai.network.base import Connected, Message, Text
from gensokyoai.room import Room
from gensokyoai.ws_server import Gateway


class _Mind:
    name = "觉"

    async def step(self, feed: dict, scene=None) -> Reply:
        return Reply()


class _BareMouth:
    """只有 send、不支持广播的嘴（ConsoleMouth 的同类）。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, name: str, text: str) -> None:
        self.sent.append((name, text))


class _FakeConn:
    def __init__(self, path: str = "g1", query: dict | None = None) -> None:
        self.path = path
        self.query = query or {}
        self.remote = "127.0.0.1"
        self.sent: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        pass


def _hub(mouth_factory=_BareMouth) -> Hub:
    return Hub(lambda rid: Loop(Room(rid), _Mind(), mouth_factory()))


# ---------- 任务自愈 ----------


async def test_hub_restarts_dead_task():
    """房间循环任务退出：get() 原地重启，不把死循环发给调用方"""
    hub = _hub()
    loop = hub.get("g1")
    old = hub._tasks["g1"]
    old.cancel()
    with pytest.raises(asyncio.CancelledError):
        await old

    async def _boom() -> None:
        raise RuntimeError("旧任务死于异常")

    dead = asyncio.create_task(_boom())
    with pytest.raises(RuntimeError):  # 等它死掉，顺带取走异常
        await dead
    hub._tasks["g1"] = dead

    revived = hub.get("g1")

    assert revived is loop, "同一个 Loop（房间内容与记忆都保住）"
    assert hub._tasks["g1"] is not dead and not hub._tasks["g1"].done()
    await hub.shutdown()


# ---------- 网关：上限与能力降级 ----------


async def test_max_rooms_rejects_new_channel_only():
    """超上限只拒新频道；已在的房间照常进"""
    hub = _hub()
    gateway = Gateway(hub, max_rooms=1)

    assert await gateway.validate(_Pending("g1")) is None
    await gateway.handle(Connected(_FakeConn("g1")))
    rejected = await gateway.validate(_Pending("g2"))

    assert rejected is not None and rejected[0] == 503
    assert await gateway.validate(_Pending("g1")) is None
    await hub.shutdown()


class _Pending:
    def __init__(self, path: str) -> None:
        self.path = path
        self.query: dict = {}
        self.headers: dict = {}
        self.remote = "127.0.0.1"


async def test_gateway_tolerates_mouth_without_broadcast():
    """接了个不支持广播的嘴：安静降级，不在连接/断开路径上抛异常"""
    hub = _hub()
    gateway = Gateway(hub)
    conn = _FakeConn("g1")

    await gateway.handle(Connected(conn))
    await gateway.handle(Message(conn, Text("你好")))

    assert hub.get("g1").room.events[0].text == "你好"
    await hub.shutdown()


async def test_mouth_capability_split():
    """ConsoleMouth 满足 Mouth，不满足 BroadcastMouth"""
    assert isinstance(ConsoleMouth(), Mouth)
    assert not isinstance(ConsoleMouth(), BroadcastMouth)
    assert isinstance(WsMouth(), BroadcastMouth)


# ---------- 记忆：坏行与原子重写 ----------


def test_memory_skips_corrupt_lines(tmp_path):
    """坏行跳过，其余照常加载"""
    path = tmp_path / "m.jsonl"
    path.write_text(
        json.dumps({"text": "好的一行", "importance": 0.5, "ts": 1.0})
        + "\n{这一行坏了\n"
        + json.dumps({"text": "又一行好的", "importance": 0.5, "ts": 2.0})
        + "\n",
        encoding="utf-8",
    )

    memory = Memory(path)

    assert memory.size == 2
    assert [item.text for item in memory._items] == ["好的一行", "又一行好的"]


async def test_consolidate_leaves_no_temp_file(tmp_path):
    """整理后不留临时文件"""
    path = tmp_path / "m.jsonl"
    memory = Memory(path)
    for i in range(8):
        await memory.note(f"旧记忆{i}", 0.5)

    await memory.consolidate(_DigestModel(), keep=3)

    assert not (tmp_path / "m.jsonl.tmp").exists()
    assert Memory(path).size == 4


async def test_consolidate_failure_keeps_original_file(tmp_path, monkeypatch):
    """替换失败时旧文件不变"""
    path = tmp_path / "m.jsonl"
    memory = Memory(path)
    for i in range(8):
        await memory.note(f"旧记忆{i}", 0.5)
    before = path.read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("替换失败")

    monkeypatch.setattr("gensokyoai.memory.os.replace", _boom)
    with pytest.raises(OSError):
        await memory.consolidate(_DigestModel(), keep=3)

    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "m.jsonl.tmp").exists()


class _DigestModel:
    async def chat(self, system: str, user: str, **kwargs) -> Reply:
        return Reply(text="概要：帕秋莉借书，恋来过。")


# ---------- 投递失败 ----------


class _FlakyMouth:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = True

    async def send(self, name: str, text: str) -> None:
        if self.fail:
            self.fail = False
            raise ConnectionError("广播通道抖了一下")
        self.sent.append((name, text))


class _SayMind:
    name = "觉"

    def __init__(self) -> None:
        self.calls = 0

    async def step(self, feed: dict, scene=None) -> Reply:
        self.calls += 1
        return Reply(calls=(ToolCall(name="send_message", arguments={"text": "在。"}),))


async def test_loop_survives_mouth_failure_and_stays_alive():
    """投递失败一次：循环不死，后续仍能回应"""
    room = Room("g1")
    mouth = _FlakyMouth()
    loop = Loop(room, _SayMind(), mouth, retry_backoff=0.01, retry_backoff_max=0.02)
    task = asyncio.create_task(loop.run())

    loop.notify("message", "帕秋莉", "在吗", user_id="u1")
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert mouth.sent == [("觉", "在。")]

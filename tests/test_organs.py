"""器官测试 —— memory / dmn / hub / mouth / style / mind。"""

import asyncio

from gensokyoai.dmn import DmnKeeper
from gensokyoai.hub import Hub
from gensokyoai.loop import Loop
from gensokyoai.memory import Memory
from gensokyoai.mind import Mind
from gensokyoai.model import Reply
from gensokyoai.mouth import WsMouth
from gensokyoai.room import Room
from gensokyoai.style import REPLY_STYLES, ReplyStyle, resolve_reply_style


class _Sink:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def send(self, frame: dict) -> None:
        self.frames.append(frame)


class _QuietMind:
    name = "觉"

    async def step(self, feed: dict) -> Reply:
        return Reply()


# ---------- memory ----------


async def test_memory_note_threshold_and_recall(tmp_path):
    """低强度不记；关键词召回命中"""
    memory = Memory(tmp_path / "m.jsonl")
    await memory.note("帕秋莉借走了《心理描写技法》", 0.8)
    await memory.note("哈哈", 0.2)
    assert memory.size == 1
    hits = await memory.recall("帕秋莉最近借了什么书")
    assert hits and "心理描写技法" in hits[0]


async def test_memory_jsonl_roundtrip(tmp_path):
    """落盘后可重新加载"""
    path = tmp_path / "m.jsonl"
    await Memory(path).note("恋昨天来过", 0.6)
    assert Memory(path).size == 1


class _DigestModel:
    async def chat(self, system: str, user: str, **kwargs) -> Reply:
        return Reply(text="概要：帕秋莉借书，恋来过。")


async def test_memory_consolidate_compresses_old(tmp_path):
    """窗口整理：旧记忆压成概要，最近条原样保留"""
    path = tmp_path / "m.jsonl"
    memory = Memory(path)
    for i in range(30):
        await memory.note(f"旧记忆{i}", 0.5)

    await memory.consolidate(_DigestModel(), keep=5)

    assert memory.size == 6
    assert "往事概要" in memory._items[0].text and "帕秋莉借书" in memory._items[0].text
    assert Memory(path).size == 6


# ---------- dmn ----------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_dmn_runs_task_once_per_sleep():
    """入睡的房间跑一次整理；醒着不跑；同一觉不重复"""
    clock = _Clock()
    keeper = DmnKeeper(idle_s=100, clock=clock)
    runs: list[str] = []

    async def tidy() -> None:
        runs.append("g1")

    activity = {"ts": clock()}
    keeper.watch("g1", lambda: activity["ts"], tidy)

    await keeper.patrol()
    assert runs == []

    activity["ts"] = clock() - 200
    await keeper.patrol()
    await keeper.patrol()

    assert runs == ["g1"]


# ---------- hub ----------


def _hub() -> Hub:
    return Hub(lambda rid: Loop(Room(rid), _QuietMind(), _Sink()))


def test_hub_creates_and_reuses_loops():
    hub = _hub()

    async def scenario():
        loop = hub.get("g1")
        assert hub.get("g1") is loop
        assert hub.room_ids() == ["g1"]
        await hub.shutdown()

    asyncio.run(scenario())


def test_hub_peek_does_not_create():
    hub = _hub()
    assert hub.peek("g1") is None and hub.has("g1") is False
    assert hub.room_ids() == []


# ---------- mouth ----------


async def test_ws_mouth_broadcast_and_strip():
    """广播到所有连接；禁叙述风格出口剥括号块"""
    mouth = WsMouth(ReplyStyle(pov="first", allow_narration=False))
    first, second = _Sink(), _Sink()
    mouth.attach(first)
    mouth.attach(second)

    await mouth.send("觉", "（第三只眼转动）你好。")

    assert first.frames[0]["text"] == "你好。"
    assert second.frames == first.frames


def test_ws_mouth_style_first_attach_wins():
    """风格首连生效，后到的声明不覆盖"""
    mouth = WsMouth()
    mouth.set_style(REPLY_STYLES["pure_dialogue"])
    mouth.set_style(REPLY_STYLES["rpg"])
    assert mouth._style.pov == "first"


def test_unknown_style_raises():
    """未知风格 fail fast，并把可选值列出来"""
    try:
        resolve_reply_style("不存在")
    except KeyError as err:
        assert "pure_dialogue" in str(err)
    else:
        raise AssertionError("应该抛 KeyError")


# ---------- mind ----------


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.closed = False

    async def chat(self, system: str, user: str, **kwargs) -> Reply:
        self.calls.append({"system": system, "user": user, **kwargs})
        return Reply()

    async def close(self) -> None:
        self.closed = True


async def test_mind_step_carries_protocol_persona_and_tools():
    """一次 step = 一次模型调用，带协议、人设与工具声明"""
    model = _FakeModel()
    mind = Mind(model, "觉", "【觉】\n地灵殿的主人")

    await mind.step(
        {"now": "2026-10-21 18:40", "events": [], "gap": None, "memories": []}
    )

    call = model.calls[0]
    assert "send_message" in call["system"] and "wait" in call["system"]
    assert "地灵殿的主人" in call["system"]
    assert "绝不可以输出正文" in call["system"]
    assert call["system"].index("地灵殿的主人") < call["system"].index(
        "绝不可以输出正文"
    )
    assert call["tools"], "必须带工具声明"
    assert '"now"' in call["user"]


async def test_mind_close_closes_model():
    model = _FakeModel()
    await Mind(model, "觉", "人设").close()
    assert model.closed is True

"""Agent 回合测试 —— 模型用工具决定说不说、说几条、等多久。"""

import asyncio
from typing import ClassVar

from gensokyoai.loop import Loop, SceneContext
from gensokyoai.model import Reply, ToolCall
from gensokyoai.room import Room


class _ScriptedMind:
    """按脚本返回动作的假心智。"""

    name = "觉"

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.feeds: list[dict] = []
        self.images: list[list] = []

    async def step(self, feed: dict, scene=None, images=None) -> Reply:
        self.feeds.append(feed)
        self.images.append(list(images or []))
        return self._replies.pop(0) if self._replies else Reply()


class _Mouth:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail = fail_times

    async def send(self, name: str, text: str) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise ConnectionError("广播通道抖了一下")
        self.sent.append((name, text))


class _Memory:
    def __init__(self, items: list[str] | None = None) -> None:
        self.items = list(items or [])
        self.queries: list[str] = []
        self.noted: list[tuple[str, float]] = []
        self.fail_note = False

    async def recall(self, query: str, limit: int = 3) -> list[str]:
        self.queries.append(query)
        return list(self.items)

    async def note(self, text: str, importance: float) -> None:
        if self.fail_note:
            raise OSError("磁盘满了")
        self.noted.append((text, importance))


def _say(text: str) -> Reply:
    return Reply(calls=(ToolCall(name="send_message", arguments={"text": text}),))


def _wait(ms: int) -> Reply:
    return Reply(calls=(ToolCall(name="wait", arguments={"ms": ms}),))


def _room(*texts: str) -> Room:
    room = Room("g1")
    for text in texts:
        room.append("message", "帕秋莉", text, user_id="u1")
    return room


def _quiet_scene(mouth: _Mouth, **wake) -> object:
    """按给定的唤醒策略造一个最小场景。"""

    class _Scene:
        _gensokyo_scene: ClassVar[dict] = {
            "name": "t_quiet",
            "prompt": "",
            "bubbles": {"max": 1, "gap_s": 0.0},
            "actions": ("say",),
            "wake": {"on_mention": 1.0, "on_others": 1.0, "cooldown_s": 0.0, **wake},
        }

        def __init__(self, ctx) -> None:
            self.ctx = ctx

        async def say(self, segments, reply_to=None) -> None:
            await self.ctx.mouth.send(self.ctx.name, segments[0]["text"])

    return _Scene(SceneContext(mouth=mouth, name="觉", room="g1", log=None))


async def test_turn_sends_what_model_says():
    """模型调 send_message -> 投递 + 入房"""
    room = _room("觉，在吗")
    mind, mouth = _ScriptedMind(_say("在。")), _Mouth()
    loop = Loop(room, mind, mouth)

    await loop._turn()

    assert mouth.sent == [("觉", "在。")]
    assert room.events[-1].kind == "reply" and room.events[-1].text == "在。"


async def test_multiple_messages_in_one_turn():
    """一轮里连调几次 send_message = 连发几条（这就是节奏的来源）"""
    room = _room("在吗")
    reply = Reply(
        calls=(
            ToolCall(name="send_message", arguments={"text": "在。"}),
            ToolCall(name="send_message", arguments={"text": "怎么了？"}),
        )
    )
    mouth = _Mouth()
    loop = Loop(room, _ScriptedMind(reply), mouth)

    await loop._turn()

    assert [text for _, text in mouth.sent] == ["在。", "怎么了？"]


async def test_no_calls_ends_turn_silently():
    """模型不调工具 = 这轮不动"""
    mouth = _Mouth()
    mind = _ScriptedMind(Reply(text="……"))
    loop = Loop(_room("随便说说"), mind, mouth)

    await loop._turn()

    assert mouth.sent == []
    assert len(mind.feeds) == 1


async def test_feed_shape():
    """投递的输入带 now / events / gap / memories"""
    memory = _Memory(["上次借走了书"])
    mind = _ScriptedMind(Reply())
    loop = Loop(_room("觉，我该看什么书？"), mind, _Mouth(), memory=memory)

    await loop._turn()

    feed = mind.feeds[0]
    assert set(feed) == {"now", "events", "gap", "history", "memories", "results"}
    assert feed["events"][0]["text"] == "觉，我该看什么书？"
    assert feed["gap"] is None
    assert feed["memories"] == ["上次借走了书"]
    assert memory.queries == ["觉，我该看什么书？"]


async def test_wait_stops_when_quiet():
    """模型只说 wait 且没人说话 -> 不再继续调模型"""
    mind = _ScriptedMind(_wait(10))
    loop = Loop(_room("嗯"), mind, _Mouth())

    await loop._turn()

    assert len(mind.feeds) == 1


async def test_wait_continues_when_message_arrives():
    """wait 期间来了新消息 -> 用新消息再调一次模型"""
    room = _room("在吗")
    mind = _ScriptedMind(_wait(5000), _say("来了。"))
    mouth = _Mouth()
    loop = Loop(room, mind, mouth)

    async def later():
        await asyncio.sleep(0.02)
        loop.notify("message", "帕秋莉", "喂？", user_id="u1")

    task = asyncio.create_task(later())
    await loop._turn()
    await task

    assert len(mind.feeds) == 2
    assert mind.feeds[1]["events"][0]["text"] == "喂？"
    assert mouth.sent == [("觉", "来了。")]


async def test_read_events_reveals_gap():
    """read_events 展开 gap，下一轮就能看见被折叠的消息"""
    room = Room("g1")
    for i in range(10):
        room.append("message", "甲", f"第{i}条")
    reply = Reply(
        calls=(
            ToolCall(
                name="read_events",
                arguments={"gap_id": 0, "side": "earliest", "limit": 3},
            ),
        )
    )
    mind = _ScriptedMind(reply, Reply())
    loop = Loop(room, mind, _Mouth(), window=2)

    await loop._turn()

    first, second = mind.feeds
    assert first["gap"]["gapId"] == 0 and first["gap"]["skipped"] == 8
    assert [event["text"] for event in second["events"]] == ["第0条", "第1条", "第2条"]
    assert second["gap"] == {"gapId": 1, "skipped": 5, "mentioned": False}


async def test_memory_note_uses_message_text():
    """记忆写的是用户原话，不是模型自己的输出"""
    memory = _Memory()
    loop = Loop(
        _room("觉，我该看什么书？"),
        _ScriptedMind(_say("随便挑一本。")),
        _Mouth(),
        memory=memory,
    )

    await loop._turn()

    text, importance = memory.noted[0]
    assert text.startswith("觉，我该看什么书？")
    assert "随便挑一本。" in text
    assert importance > 0


async def test_memory_failure_does_not_block_send():
    """记忆坏了只告警，消息照发"""
    memory = _Memory()
    memory.fail_note = True
    mouth = _Mouth()
    loop = Loop(_room("在吗"), _ScriptedMind(_say("在。")), mouth, memory=memory)

    await loop._turn()

    assert mouth.sent == [("觉", "在。")]


async def test_run_retries_after_failure():
    """模型第一次抛异常：退避重试同一批消息，不丢消息、不重复发送"""

    class _Flaky:
        name = "觉"

        def __init__(self) -> None:
            self.calls = 0

        async def step(self, feed, scene=None, images=None):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("后端断线")
            return _say("在。")

    room = _room("在吗")
    mind, mouth = _Flaky(), _Mouth()
    loop = Loop(room, mind, mouth, retry_backoff=0.01, retry_backoff_max=0.02)
    task = asyncio.create_task(loop.run())
    loop.notify("message", "帕秋莉", "喂？", user_id="u1")
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert mind.calls == 2
    assert mouth.sent == [("觉", "在。")], "重试不该把已经发出去的那条再发一遍"


async def test_feed_carries_recent_context(tmp_path):
    """每轮都给上下文：重启前聊过什么、自己刚说了什么都要看得到"""
    from gensokyoai.bgtasks import BgTasks
    from gensokyoai.room import RoomLog

    tasks = BgTasks()
    path = tmp_path / "events.jsonl"
    before = Room("g1", log=RoomLog(path, tasks))
    before.append("message", "帕秋莉", "上次说的那本书", user_id="u1")
    before.append("reply", "觉", "记着")
    await tasks.idle()

    room = Room("g1", log=RoomLog(path, tasks))
    room.append("message", "帕秋莉", "我又来了", user_id="u1")
    mind = _ScriptedMind(_say("在的。"), Reply())
    loop = Loop(room, mind, _Mouth())

    await loop._turn()
    room.append("message", "帕秋莉", "还在吗", user_id="u1")
    await loop._turn()

    # 重启前的对话作为上下文
    assert [event["text"] for event in mind.feeds[0]["history"]] == [
        "上次说的那本书",
        "记着",
    ]
    # 当前批次不重复出现在上下文里
    assert [event["text"] for event in mind.feeds[0]["events"]] == ["我又来了"]
    # 第二轮：上下文接上了上一轮的问与答（更早的也在，窗口内）
    tail = [event["text"] for event in mind.feeds[1]["history"]][-2:]
    assert tail == ["我又来了", "在的。"]
    assert "还在吗" not in [event["text"] for event in mind.feeds[1]["history"]]
    assert [event["text"] for event in mind.feeds[1]["events"]] == ["还在吗"]


async def test_same_line_is_not_repeated(tmp_path):
    """同一句在去重窗口里只发一次（模型抽风时的兜底）"""
    mouth = _Mouth()
    loop = Loop(
        _room("在吗"),
        _ScriptedMind(Reply()),
        mouth,
        dedup_window=60.0,
    )

    await loop._say([{"type": "text", "text": "天人的效率总是惊人。"}])
    await loop._say([{"type": "text", "text": "天人的效率总是惊人。"}])
    await loop._say([{"type": "text", "text": "另一句。"}])

    assert [text for _, text in mouth.sent] == ["天人的效率总是惊人。", "另一句。"]


async def test_unmentioned_message_does_not_wake_quiet_scene():
    """没被点名 + 唤醒概率 0：消息入房，但不开一轮"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=0.0)
    loop = Loop(Room("g1"), _ScriptedMind(Reply()), mouth, scene=scene, rng=lambda: 0.0)

    loop.notify("message", "帕秋莉", "今天好热", user_id="u1")

    assert loop.room.unread == 1, "消息照样入房，上下文不丢"
    assert not loop._wake.is_set(), "但没醒"


async def test_mentioned_message_always_wakes():
    """被点名：唤醒概率 1，一定醒"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=0.0)
    loop = Loop(
        Room("g1"), _ScriptedMind(Reply()), mouth, scene=scene, rng=lambda: 0.99
    )

    loop.notify("message", "帕秋莉", "觉，在吗", user_id="u1", mentioned=True)

    assert loop._wake.is_set()


async def test_cooldown_blocks_unmentioned_after_speaking():
    """刚说过话的冷却期内，没被点名就不醒"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth, on_others=1.0, cooldown_s=60.0)
    loop = Loop(Room("g1"), _ScriptedMind(Reply()), mouth, scene=scene, rng=lambda: 0.0)

    await loop._say([{"type": "text", "text": "嗯。"}])
    loop.notify("message", "帕秋莉", "随便说说", user_id="u1")

    assert not loop._wake.is_set(), "冷却期内不许醒"
    loop.notify("message", "帕秋莉", "觉，你呢", user_id="u1", mentioned=True)
    assert loop._wake.is_set(), "点名可以穿透冷却"


async def test_images_reach_the_model():
    """带图的消息：房间事件记了图，图片作为多模态附件给模型"""
    mind = _ScriptedMind(Reply())
    loop = Loop(Room("g1"), mind, _Mouth())

    loop.notify(
        "message",
        "帕秋莉",
        "看这张 [图片×1]",
        user_id="u1",
        images=("data:image/png;base64,AAAA",),
    )
    await loop._turn()

    assert mind.feeds[0]["events"][0]["images"] == 1
    assert mind.images[0] == [("帕秋莉 发的第 1 张图：", "data:image/png;base64,AAAA")]


async def test_prose_is_dropped_by_default():
    """没调工具、只写正文：默认丢掉（协议优先）"""
    mouth = _Mouth()
    mind = _ScriptedMind(Reply(text="我在。"))
    loop = Loop(Room("g1"), mind, mouth)

    loop.notify("message", "帕秋莉", "在吗", user_id="u1")
    await loop._turn()

    assert mouth.sent == []


async def test_prose_fallback_sends_when_scene_allows():
    """场景声明 on_prose=send：正文当作一句话发出去，别让用户干等"""
    mouth = _Mouth()
    scene = _quiet_scene(mouth)
    scene._gensokyo_scene = {**scene._gensokyo_scene, "on_prose": "send"}
    loop = Loop(Room("g1"), _ScriptedMind(Reply(text="绿色。")), mouth, scene=scene)

    loop.notify("message", "帕秋莉", "这图什么颜色", user_id="u1")
    await loop._turn()

    assert mouth.sent == [("觉", "绿色。")]
    assert loop.room.events[-1].kind == "reply"

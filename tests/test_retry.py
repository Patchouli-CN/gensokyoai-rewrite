"""回合故障：gap 回滚、精确放弃与退避期间的新消息。"""

import asyncio

from gensokyoai.loop import Loop
from gensokyoai.model import Reply
from gensokyoai.room import Room


class _Mouth:
    async def send(self, name: str, text: str) -> None:
        pass


class _FlakyMind:
    name = "觉"
    prompt_cache = None

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.feeds: list[dict] = []

    async def step(self, feed, scene=None, images=None):
        self.calls += 1
        self.feeds.append(feed)
        if self.calls <= self.failures:
            raise ConnectionError("后端暂时不可用")
        return Reply()


async def _wait_calls(mind: _FlakyMind, count: int) -> None:
    """等待真实 run 循环推进到指定调用次数。"""
    for _ in range(100):
        if mind.calls >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"模型只调用了 {mind.calls} 次，预期至少 {count} 次")


def test_push_back_restores_shown_and_gap():
    """失败批次退回后，重新取 feed 仍得到原窗口和原跳过数量"""
    room = Room("g1")
    for index in range(5):
        room.append("message", "甲", f"第{index}条")
    shown, gap = room.take_feed(window=2)

    recovered = room.push_back(shown, gap_ids={gap["gapId"]})

    assert recovered == {1, 2, 3, 4, 5}
    assert room.unread == 5
    assert room.reveal(gap["gapId"]) == ([], None)
    again, new_gap = room.take_feed(window=2)
    assert [event.text for event in again] == ["第3条", "第4条"]
    assert new_gap["skipped"] == 3


def test_trim_also_releases_gap_events():
    """事件流裁剪后，gap 不再偷偷持有已经被淘汰的旧事件"""
    room = Room("g1", max_events=3)
    for index in range(3):
        room.append("message", "甲", f"第{index}条")
    room.take_feed(window=1)
    assert room._gaps

    for index in range(3, 5):
        room.append("message", "甲", f"第{index}条")

    assert room._gaps == {}
    assert [event.text for event in room.events] == ["第2条", "第3条", "第4条"]


async def test_retry_restores_original_gap():
    """真实 run 第一次失败后，第二次仍看到同一窗口与 gap"""
    room = Room("g1")
    for index in range(4):
        room.append("message", "甲", f"第{index}条")
    mind = _FlakyMind(failures=1)
    loop = Loop(room, mind, _Mouth(), window=2, retry_backoff=0)
    task = asyncio.create_task(loop.run())
    try:
        loop.notify("message", "甲", "第4条")
        await _wait_calls(mind, 2)
        await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert mind.feeds[0]["gap"]["gapId"] != mind.feeds[1]["gap"]["gapId"]
    assert mind.feeds[1]["gap"]["skipped"] == 3
    assert [event["text"] for event in mind.feeds[1]["events"]] == ["第3条", "第4条"]
    assert len(room._gaps) == 1, "失败时作废的 gap 不能残留"


async def test_give_up_does_not_drop_messages_arriving_during_backoff():
    """原批次重试到顶后，退避期间的新消息仍留给下一轮"""
    mind = _FlakyMind(failures=2)
    loop = Loop(
        Room("g1"),
        mind,
        _Mouth(),
        max_attempts=2,
        retry_backoff=0.1,
        retry_backoff_max=0.1,
    )
    task = asyncio.create_task(loop.run())
    try:
        loop.notify("message", "甲", "坏批次")
        await _wait_calls(mind, 1)
        loop.notify("message", "乙", "退避期间的新消息")
        await _wait_calls(mind, 3)
        await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert [event["text"] for event in mind.feeds[-1]["events"]] == ["退避期间的新消息"]
    assert loop.room.unread == 0

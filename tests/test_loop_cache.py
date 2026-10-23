"""Loop 与 PromptCache 的接法：history 让位给 transcript，轮末推入前缀。"""

import asyncio

from gensokyoai.llm.prompt_cache import PromptCache
from gensokyoai.loop import Loop
from gensokyoai.model import Reply
from gensokyoai.room import Room


class _Mouth:
    def __init__(self) -> None:
        self.said: list[str] = []

    async def say(self, text: str) -> None:
        self.said.append(text)


class _Scene:
    _gensokyo_scene = {  # noqa: RUF012 - 场景声明本来就是类属性
        "name": "test",
        "prompt": "测试场景",
        "actions": ("say",),
    }

    async def say(self, text: str) -> None:
        pass


class _Mind:
    """记下 feed，并带上真实的前缀缓存。"""

    name = "觉"

    def __init__(self) -> None:
        self.feeds: list[dict] = []
        self.prompt_cache = PromptCache(align=0, name="test")

    async def step(self, feed, scene=None, images=None):
        self.feeds.append(feed)
        return Reply()


async def test_feed_has_no_history_when_cache_on():
    """开着前缀缓存时，history 走 transcript，不进 JSON"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    loop.notify("message", "帕秋莉", "在吗", user_id="u1")

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert mind.feeds, "应该跑过一轮"
    assert "history" not in mind.feeds[0], "有缓存时不该再塞 history"


async def test_feed_keeps_history_without_cache():
    """没有前缀缓存时保持原样（history 在 JSON 里）"""
    room = Room("g1")
    mind = _Mind()
    mind.prompt_cache = None
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    loop.notify("message", "帕秋莉", "在吗", user_id="u1")

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "history" in mind.feeds[0]


async def test_turn_end_pushes_messages_into_prefix():
    """一轮结束后，房间里的话进入 transcript（下次就不用再传 history）"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    loop.notify("message", "帕秋莉", "在吗", user_id="u1")

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    transcript = mind.prompt_cache.transcript()
    assert "帕秋莉：在吗" in transcript


async def test_sync_is_idempotent():
    """同一批消息不会重复进前缀"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    room.append("message", "帕秋莉", "在吗", user_id="u1")

    await loop._sync_cache()
    await loop._sync_cache()

    assert mind.prompt_cache.transcript().count("帕秋莉：在吗") == 1

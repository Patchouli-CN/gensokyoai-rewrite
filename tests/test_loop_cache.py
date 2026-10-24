"""Loop 与 PromptCache 的接法：history 让位给 transcript，轮末推入前缀。"""

import asyncio

from gensokyoai.llm.prompt_cache import PromptCache
from gensokyoai.loop import Loop
from gensokyoai.mind import Mind
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
    """同一批已处理消息不会重复进前缀"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    room.append("message", "帕秋莉", "在吗", user_id="u1")
    room.take_feed()

    await loop._sync_cache()
    await loop._sync_cache()

    assert mind.prompt_cache.transcript().count("帕秋莉：在吗") == 1


async def test_sync_waits_for_messages_arriving_during_turn():
    """回合期间新到的未读不能同时成为历史和下一轮当前事件"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    room.append("message", "帕秋莉", "本轮消息")
    room.take_feed()
    await loop._sync_cache()

    room.append("message", "帕秋莉", "等待期间的新消息")
    await loop._sync_cache()

    assert "等待期间的新消息" not in mind.prompt_cache.transcript()
    room.take_feed()
    await loop._sync_cache()
    assert mind.prompt_cache.transcript().count("等待期间的新消息") == 1


async def test_sync_waits_for_unrevealed_gap():
    """折进 gap 的旧消息只有展开后才进入稳定历史"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    for text in ("旧一", "旧二", "当前"):
        room.append("message", "帕秋莉", text)
    _, gap = room.take_feed(window=1)

    await loop._sync_cache()

    transcript = mind.prompt_cache.transcript()
    assert "当前" in transcript
    assert "旧一" not in transcript and "旧二" not in transcript
    room.reveal(gap["gapId"])
    await loop._sync_cache()
    transcript = mind.prompt_cache.transcript()
    assert "旧一" in transcript and "旧二" in transcript


class _RequestModel:
    capabilities = frozenset()
    context = 1000

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    async def chat(self, system, user, **kwargs):
        self.requests.append((system, user))
        return Reply()


async def test_mind_sends_cached_history_only_once():
    """真实 Mind 的 system 有历史时，user 不再重复同一份 transcript"""
    model = _RequestModel()
    cache = PromptCache(align=0, name="test")
    cache.add("帕秋莉：旧话")
    mind = Mind(model, "觉", "人设", prompt_cache=cache)

    await mind.step({"events": [{"text": "新话"}]})

    system, user = model.requests[0]
    assert system.count("帕秋莉：旧话") == 1
    assert "帕秋莉：旧话" not in user
    assert "新话" in user

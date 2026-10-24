"""房间隔离：共享模型端点不等于共享聊天上下文。"""

from gensokyoai.__main__ import _loop_factory
from gensokyoai.llm.prompt_cache import PromptCache
from gensokyoai.mind import Mind


class _Model:
    capabilities = frozenset()
    context = 1000


class _Mouth:
    async def send(self, name: str, text: str) -> None:
        pass


async def test_prompt_cache_and_compaction_are_room_scoped():
    """A/B 房各有一份缓存，B 房压缩不能改写 A 房历史"""
    model = _Model()
    prototype = Mind(
        model, "觉", "人设", prompt_cache=PromptCache(align=0, name="test")
    )
    factory = _loop_factory(prototype, lambda _room: _Mouth(), None)
    room_a = factory("room-a")
    room_b = factory("room-b")

    assert room_a.mind is not room_b.mind
    assert room_a.mind.model is model and room_b.mind.model is model
    assert room_a.mind.prompt_cache is not room_b.mind.prompt_cache

    room_a.room.append("message", "甲", "只属于 A 房的话")
    room_b.room.append("message", "乙", "只属于 B 房的话")
    room_a.room.take_feed()
    room_b.room.take_feed()
    await room_a._sync_cache()
    await room_b._sync_cache()

    transcript_a = room_a.mind.prompt_cache.transcript()
    transcript_b = room_b.mind.prompt_cache.transcript()
    assert "只属于 A 房的话" in transcript_a
    assert "只属于 B 房的话" not in transcript_a
    assert "只属于 B 房的话" in transcript_b
    assert "只属于 A 房的话" not in transcript_b

    room_b.mind.prompt_cache.compact("B 房概要")

    assert room_a.mind.prompt_cache.transcript() == transcript_a
    assert room_b.mind.prompt_cache.transcript() == "B 房概要"

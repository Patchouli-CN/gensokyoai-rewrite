"""压缩路径的测试：阈值触发、整理期压缩、摘要进历史。"""

from gensokyoai.llm.prompt_cache import PromptCache
from gensokyoai.loop import Loop
from gensokyoai.model import Reply
from gensokyoai.room import Room


class _Mouth:
    async def say(self, text: str) -> None:
        pass


class _Scene:
    _gensokyo_scene = {  # noqa: RUF012 - 场景声明就是类属性
        "name": "test",
        "prompt": "测试",
        "actions": ("say",),
    }


class _Model:
    name = "stub"
    context = 1000

    async def chat(self, system, user, **kwargs):
        del system, user, kwargs
        return Reply(text="（这是概要）")


class _Mind:
    name = "觉"

    def __init__(self) -> None:
        self.model = _Model()
        self.cheap_model = self.model
        self.prompt_cache = PromptCache(fixed=0, name="t")

    async def step(self, feed, scene=None, images=None):
        del feed, scene, images
        return Reply()


async def test_compact_cache_replaces_old_history():
    """压缩把旧历史换成摘要，保留最近几条"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    for index in range(20):
        mind.prompt_cache.add(f"甲：第{index}条" * 5)
        mind.prompt_cache.stabilize()

    dropped = await loop.compact_cache(keep=3)

    assert dropped > 0
    blocks = mind.prompt_cache.history_blocks
    assert blocks[0].startswith("（往事概要）")
    assert len(blocks) == 4  # 摘要 + 3 条


async def test_maybe_compact_respects_threshold():
    """没到窗口阈值就不压"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    for index in range(5):
        mind.prompt_cache.add(f"甲：短消息{index}")
    mind.prompt_cache.stabilize()

    assert await loop.maybe_compact() == 0


async def test_maybe_compact_fires_over_threshold():
    """超过窗口 75% 就压"""
    room = Room("g1")
    mind = _Mind()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    for _ in range(40):
        mind.prompt_cache.add("甲：" + "字" * 40)
        mind.prompt_cache.stabilize()
    assert mind.prompt_cache.total_tokens > mind.model.context * 0.75

    assert await loop.maybe_compact() > 0


async def test_compact_without_cache_is_noop():
    """没装前缀缓存时压缩是空操作"""
    room = Room("g1")
    mind = _Mind()
    mind.prompt_cache = None
    loop = Loop(room, mind, _Mouth(), scene=_Scene())

    assert await loop.compact_cache() == 0
    assert await loop.maybe_compact() == 0


async def test_compact_survives_model_failure():
    """概要模型挂了也要能压（退化成直接丢弃旧历史）"""

    class _Broken(_Model):
        async def chat(self, system, user, **kwargs):
            del system, user, kwargs
            raise ConnectionError("断线")

    room = Room("g1")
    mind = _Mind()
    mind.cheap_model = _Broken()
    loop = Loop(room, mind, _Mouth(), scene=_Scene())
    for index in range(10):
        mind.prompt_cache.add(f"甲：第{index}条" * 5)
        mind.prompt_cache.stabilize()

    dropped = await loop.compact_cache(keep=2)

    assert dropped > 0
    assert len(mind.prompt_cache.history_blocks) == 2

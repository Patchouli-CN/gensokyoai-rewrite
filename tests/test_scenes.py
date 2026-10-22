"""场景边界 —— 同一个 Loop 跑不同场景，回合一致、出口与策略不同。"""

from gensokyoai.api import scene, tool
from gensokyoai.loop import Loop, SceneContext
from gensokyoai.model import Reply, ToolCall
from gensokyoai.room import Room


class _Mouth:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, name: str, text: str) -> None:
        self.sent.append(text)


class _ScriptedMind:
    prompt_cache = None
    name = "觉"

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.feeds: list[dict] = []

    async def step(self, feed: dict, scene=None, images=None) -> Reply:
        self.feeds.append(feed)
        return self._replies.pop(0) if self._replies else Reply()


@scene(
    name="t_group",
    prompt="你在一个群里。",
    bubbles={"max": 3, "gap_s": 0.0},
    actions=("say", "wait", "read_events"),
)
class GroupScene:
    def __init__(self, ctx) -> None:
        self.ctx = ctx

    async def say(self, segments, reply_to=None) -> None:
        text = "".join(s["text"] for s in segments if s["type"] == "text")
        await self.ctx.mouth.send(self.ctx.name, text)


@scene(name="t_tavern", prompt="你在酒馆里。", bubbles={"max": 1, "gap_s": 0.0})
class TavernScene(GroupScene):
    def describe(self, event: dict) -> dict:
        return {**event, "scene": "tavern"}

    async def say(self, segments, reply_to=None) -> None:
        text = "".join(s["text"] for s in segments if s["type"] == "text")
        await self.ctx.mouth.send(self.ctx.name, f"[酒馆] {text}")

    @tool("read_member", "读一个人的名片", {"user_id": "string"})
    async def read_member(self, user_id: str):
        return [{"name": "帕秋莉", "text": f"uid={user_id}"}]


def _scene(cls, mouth, name="觉"):
    return cls(SceneContext(mouth=mouth, name=name, room="g1", log=None))


def _three_says() -> Reply:
    return Reply(
        calls=tuple(
            ToolCall(name="send_message", arguments={"text": f"第{i}句"})
            for i in range(3)
        )
    )


def _room() -> Room:
    room = Room("g1")
    room.append("message", "帕秋莉", "在吗", user_id="u1")
    return room


async def test_group_scene_sends_multiple_bubbles():
    """群聊场景：三条都发"""
    mouth = _Mouth()
    loop = Loop(
        _room(), _ScriptedMind(_three_says()), mouth, scene=_scene(GroupScene, mouth)
    )

    await loop._turn()

    assert mouth.sent == ["第0句", "第1句", "第2句"]


async def test_tavern_scene_caps_bubbles_and_prefixes():
    """酒馆场景：同一个模型输出，只发一条且带前缀"""
    mouth = _Mouth()
    loop = Loop(
        _room(), _ScriptedMind(_three_says()), mouth, scene=_scene(TavernScene, mouth)
    )

    await loop._turn()

    assert mouth.sent == ["[酒馆] 第0句"], "max=1 时多余的 send_message 被丢掉"


async def test_describe_reaches_the_model():
    """场景可以改写事件形状"""
    mouth = _Mouth()
    mind = _ScriptedMind(Reply())
    loop = Loop(_room(), mind, mouth, scene=_scene(TavernScene, mouth))

    await loop._turn()

    assert mind.feeds[0]["events"][0]["scene"] == "tavern"


async def test_scene_tool_result_goes_to_next_feed():
    """场景工具的结果回填到下一轮输入"""
    mouth = _Mouth()
    reply = Reply(calls=(ToolCall(name="read_member", arguments={"user_id": "u9"}),))
    mind = _ScriptedMind(reply, Reply())
    loop = Loop(_room(), mind, mouth, scene=_scene(TavernScene, mouth))

    await loop._turn()

    assert mind.feeds[1]["results"] == [{"name": "帕秋莉", "text": "uid=u9"}]


async def test_default_scene_is_builtin_chat():
    """不传 scene 时用内置 chat 场景"""
    mouth = _Mouth()
    loop = Loop(_room(), _ScriptedMind(_three_says()), mouth)

    await loop._turn()

    assert mouth.sent == ["第0句", "第1句", "第2句"]

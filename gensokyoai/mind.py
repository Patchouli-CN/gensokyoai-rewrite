"""Mind —— 组装提示词，把这一轮的输入交给模型，拿回它想做的动作。

一次 step = 一次模型调用。不写房间、不投递，只说"我想干什么"。
提示词由三段拼成：人设（角色卡）+ 场景（插件给的）+ 机制（内核固定）。
"""

import json

from .api import schema_of, spec_of, tools_of
from .model import ChatModel, Reply
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("MIND")

_MAX_TOKENS = 400
""" 一轮的输出上限（只需要几个工具调用） """

_ALL_ACTIONS = ("say", "wait", "read_events")

_FEED_DOC = """\
每次输入是一段 JSON：

- now：现在的时间
- events：刚到的消息，只给未读。每条有 msgId / name / userId / text / time / mentioned；
  带图的会多一个 images 字段（数字 = 张数），图本身以附件形式跟在后面
- gap：你没看到的一段消息。skipped 是里面的条数，mentioned 表示里面有人提到你
- history：这一批之前的最近几句，含你自己刚说过的话——别重复，也别接不上
- memories：你想起的旧事，可能和现在的话题有关
- results：你刚才调用工具拿到的结果

你能用的工具："""

_ACTION_DOC = {
    "say": "- send_message：发一条消息。想连说几句就多调几次，每条都短。",
    "wait": "- wait：等 ms 毫秒，或者等到下一条消息。不想接话就调它。",
    "read_events": "- read_events：prompt 里有 gap 的时候才能用，展开那一段。",
}

_HARD = """\
绝不可以输出正文。每一轮只有工具调用会生效，正文会被直接丢弃。

要说话就调 send_message；不想说话就调 wait。
没被点名时默认 wait，除非确实有你要接的话。不要为了说话而说话。"""

_CORE_TOOLS: dict[str, dict] = {
    "say": {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "向当前场景发一条消息。想连说几句就多调几次，每条都要短。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要说的话"}},
                "required": ["text"],
            },
        },
    },
    "wait": {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "等 ms 毫秒，或等到下一条消息（哪个先到算哪个）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ms": {"type": "integer", "minimum": 0, "description": "等待毫秒数"}
                },
                "required": ["ms"],
            },
        },
    },
    "read_events": {
        "type": "function",
        "function": {
            "name": "read_events",
            "description": "展开 prompt 里的一条 gap。一条只能开一次，没揭完会给新的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "gap_id": {"type": "integer"},
                    "side": {"type": "string", "enum": ["earliest", "latest"]},
                    "limit": {"type": "integer", "minimum": 0},
                },
                "required": ["gap_id", "limit"],
            },
        },
    },
}


class Mind:
    """角色的心智：人设 + 模型端点。"""

    def __init__(
        self,
        model: ChatModel,
        name: str,
        persona: str,
        vision: ChatModel | None = None,
    ) -> None:
        """初始化。

        Args:
            model: 模型端点
            name: 角色名（房间里的发言署名）
            persona: 人设正文（角色卡提供）
            vision: 带图回合改用它（None = 一直用 model）
        """
        self._model = model
        self._vision = vision
        self.name = name
        self._persona = persona

    @property
    def model(self) -> ChatModel:
        """底层模型端点（静息整理压缩记忆用）。"""
        return self._model

    async def step(
        self,
        feed: dict,
        scene=None,
        images: list[tuple[str, str]] | None = None,
    ) -> Reply:
        """把这一轮的输入交给模型。

        Args:
            feed: {now, events, gap, history, memories, results}
            scene: 场景实例（None = 只用内核默认动作）
            images: 这一批消息里带的图 `[(说明, data URL), ...]`

        Returns:
            Reply: 正文与工具调用
        """
        chosen = self._vision if images and self._vision is not None else self._model
        return await chosen.chat(
            self._system(scene),
            json.dumps(feed, ensure_ascii=False),
            tools=self._tools(scene),
            images=images,
            max_tokens=_MAX_TOKENS,
            temperature=0.85,
        )

    async def close(self) -> None:
        """关闭底层模型连接。"""
        await self._model.close()

    def _system(self, scene) -> str:
        """人设 + 场景 + 机制。顺序有讲究：人设在前、硬禁止在最后（实测）。"""
        spec = (
            spec_of(scene)
            if scene is not None
            else {"prompt": "", "actions": _ALL_ACTIONS}
        )
        lines = [_FEED_DOC]
        for action in spec["actions"]:
            lines.append(_ACTION_DOC[action])
        if scene is not None:
            for meta in tools_of(scene):
                lines.append(f"- {meta['name']}：{meta['description']}")
        lines.append("")
        lines.append(_HARD)
        body = "\n".join(lines)
        return "\n\n".join(
            part.strip()
            for part in (self._persona, spec["prompt"], body)
            if part.strip()
        )

    def _tools(self, scene) -> list[dict]:
        """按场景启用的动作拼工具声明，再挂上场景自己的工具。"""
        spec = spec_of(scene) if scene is not None else {"actions": _ALL_ACTIONS}
        tools = [_CORE_TOOLS[action] for action in spec["actions"]]
        if scene is not None:
            tools.extend(schema_of(meta) for meta in tools_of(scene))
        return tools

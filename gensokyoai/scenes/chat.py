"""内置场景：普通聊天室。

说、等、翻看错过的消息，连发最多 3 条、间隔 0.4 秒。
"""

from ..api import scene


@scene(
    name="chat",
    prompt="你在一间聊天室里。",
    bubbles={"max": 3, "gap_s": 0.4},
    actions=("say", "wait", "read_events"),
)
class ChatScene:
    """最普通的一间房。"""

    def __init__(self, ctx) -> None:
        """初始化。

        Args:
            ctx: 库给的场景上下文（mouth / name / room / log）
        """
        self.ctx = ctx

    def describe(self, event: dict) -> dict:
        """默认原样给模型看。"""
        return event

    async def say(self, segments: list[dict], reply_to: int | None = None) -> None:
        """把一句话经出口发出去。"""
        text = "".join(
            str(segment.get("text", ""))
            for segment in segments
            if segment.get("type") == "text"
        )
        if text.strip():
            await self.ctx.mouth.send(self.ctx.name, text)

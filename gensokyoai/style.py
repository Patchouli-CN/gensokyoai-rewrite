"""回复风格（ReplyStyle）—— 叙述许可 + 开场白开关。

场景归属由接入侧（插件/连接参数）声明，机制归框架：出口按 allow_narration
剥离括号叙述。人设卡不参与风格，同一张卡任意场景复用。
"""

from typing import Literal

import msgspec

type NarrativePOV = Literal["first", "third", "any"]
""" 叙事视角声明（当前只在预设里作标识，出口不据此改写文本） """


class ReplyStyle(msgspec.Struct, frozen=True):
    """回复风格：按字段组合，预设只是常用搭配。"""

    pov: NarrativePOV = "any"
    allow_narration: bool = True
    send_greeting: bool = True


REPLY_STYLES: dict[str, ReplyStyle] = {
    "free": ReplyStyle(),
    "pure_dialogue": ReplyStyle(
        pov="first", allow_narration=False, send_greeting=False
    ),
    "tavern": ReplyStyle(pov="any", allow_narration=True, send_greeting=True),
    "rpg": ReplyStyle(pov="third", allow_narration=True, send_greeting=True),
}


def resolve_reply_style(name: str) -> ReplyStyle:
    """按名取预设；未知名报错并列出可选值（fail fast 方便排错）。"""
    try:
        return REPLY_STYLES[name]
    except KeyError:
        raise KeyError(
            f"未知回复风格: {name!r}（可选: {', '.join(sorted(REPLY_STYLES))}）"
        ) from None

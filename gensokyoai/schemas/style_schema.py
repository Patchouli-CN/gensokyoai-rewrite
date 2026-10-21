"""回复风格契约 —— 叙事视角 + 叙述许可 + 开场白开关。

**场景归属由接入侧（插件）声明，机制由框架提供**：插件按场景挑一个预设
（群聊 → pure_dialogue、酒馆 → tavern、RPG → rpg），框架把声明翻译成
两个手段——responder 人设尾注【输出格式】块 + 投递层叙述剥离兜底；
开场白则由世界启动时按 `send_greeting` 门控。

人设卡不参与风格：卡只写人设，同一张卡可以在任何场景复用。
"""

from typing import Literal

import msgspec

type NarrativePOV = Literal["first", "third", "any"]
""" 叙事视角：第一人称（角色本人开口）/ 第三人称（旁白叙述）/ 不限 """


class ReplyStyle(msgspec.Struct, frozen=True):
    """回复风格：框架提供的通用输出约束原语（按字段组合，不止预设三种）。"""

    pov: NarrativePOV = "any"
    """ 叙事视角限制 """
    allow_narration: bool = True
    """ 是否允许动作/神态/场景等叙述性描写（False = 纯对话，只输出说出口的话）"""
    send_greeting: bool = True
    """ 是否在世界启动时发送角色卡开场白 """

    def format_block(self) -> str:
        """框架统一管理的【输出格式】尾注（追加在 responder 人设 system 尾部）。

        无约束（不限视角且允许叙述）时返回空串——不注入任何格式要求，
        与旧行为逐字节一致。
        """
        lines: list[str] = []
        if self.pov == "first":
            lines.append("- 以角色本人的口吻直接说话（第一人称）")
        elif self.pov == "third":
            lines.append("- 用第三人称叙述角色的言行（小说旁白式，可描写动作与场景）")
        if not self.allow_narration:
            lines.append(
                "- 只输出角色实际说出口的话：不写动作、神态、心理或场景描写，"
                "也不要用括号、星号等方式夹带叙述"
            )
        if not lines:
            return ""
        return "【输出格式】\n" + "\n".join(lines)


REPLY_STYLES: dict[str, ReplyStyle] = {
    # 默认：不限制（旧行为）
    "free": ReplyStyle(),
    # 群聊：第一人称纯对话，禁一切第三方描写，免开场白
    "pure_dialogue": ReplyStyle(pov="first", allow_narration=False, send_greeting=False),
    # 酒馆：视角不限，叙述自由
    "tavern": ReplyStyle(pov="any", allow_narration=True, send_greeting=True),
    # RPG 游戏：第三人称旁白叙述
    "rpg": ReplyStyle(pov="third", allow_narration=True, send_greeting=True),
}
""" 具名预设：接入侧按场景挑词，不必逐字段拼装 """


def resolve_reply_style(name: str) -> ReplyStyle:
    """按名取预设。

    Args:
        name: 预设名（见 REPLY_STYLES）

    Returns:
        ReplyStyle: 对应预设

    Raises:
        KeyError: 未知名（错误信息列出可选值，方便插件作者排错）
    """
    try:
        return REPLY_STYLES[name]
    except KeyError:
        raise KeyError(
            f"未知回复风格: {name!r}（可选: {', '.join(sorted(REPLY_STYLES))}）"
        ) from None


__all__ = ["REPLY_STYLES", "NarrativePOV", "ReplyStyle", "resolve_reply_style"]

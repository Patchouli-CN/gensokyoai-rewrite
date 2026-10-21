"""文本小件 —— 控制字符清洗 + 括号叙述剥离（禁叙述风格的出口兜底）。"""

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NARRATION_BLOCK = re.compile(r"（[^（）]*）|\([^()]*\)")


def strip_control_chars(text: str) -> str:
    """去除控制字符（保留换行和制表符）。"""
    return _CONTROL_CHARS.sub("", text)


def strip_narration(text: str) -> str:
    """剥掉括号包裹的动作/神态/场景描写，清理因此产生的空行。

    只管「括号块」这一最主流形态；无括号叙述句不乱猜（误伤正常对话
    比漏放一条描写更伤）。
    """
    stripped = _NARRATION_BLOCK.sub("", text)
    lines = [line.strip() for line in stripped.splitlines()]
    return "\n".join(line for line in lines if line).strip()

"""话术风格控制"""

_TONE_SUFFIX = {"愤怒": "！", "兴奋": "！", "疑问": "？"}
""" 情绪 -> 语气后缀（规则级润色，不动内容）"""


def apply_emotion_hint(text: str, emotion: str) -> str:
    """按情绪提示微调标点语气。

    Args:
        text: 待调整的回复文本
        emotion: 情绪提示（BrainConclusion.emotion）

    Returns:
        str: 调整后的文本；无匹配规则时原样返回
    """
    if not text or not emotion:
        return text
    suffix = _TONE_SUFFIX.get(emotion)
    if suffix and not text.endswith(("。", "！", "？", "…", "~")):
        return text + suffix
    return text

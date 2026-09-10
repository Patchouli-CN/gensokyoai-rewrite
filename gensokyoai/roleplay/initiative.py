"""主动发言评估 —— 原版 GensokyoAI 四维对话欲的规则化移植（零 token）。

四维（与角色卡 motivation_weights 对齐）：
- expression  表达欲：角色天性基础值（来自角色卡 expression_base，话痨 vs 沉默寡言）
- emotional   情绪唤起：最近对话中的情绪词密度
- relational  关系牵引：被点名 / 被 @ / 名字被提及
- situational 情境时机：悬而未决的问题 + 空闲时长

加权求和得到 0.0 ~ 1.0 的对话欲，达到阈值即触发主动发言。
"""

import zlib

_EMOTION_WORDS = (
    "开心",
    "难过",
    "生气",
    "高兴",
    "喜欢",
    "讨厌",
    "害怕",
    "惊讶",
    "哭",
    "笑",
    "想念",
    "担心",
)
""" 情绪唤起词表 """

_DEFAULT_WEIGHTS: dict[str, float] = {
    "expression": 0.25,
    "emotional": 0.25,
    "relational": 0.25,
    "situational": 0.25,
}
""" 四维默认等权；角色卡可用 motivation_weights 覆盖 """

_LONG_SILENCE_SECONDS = 600
""" 超过该空闲时长归于「长时间沉默」档 """

_SILENCE_OPEN_QUESTION = (
    "（那句问话还悬在半空，没人回应……）",
    "（提问的人像是走神了，空气安静了下来）",
    "（没人接话，只剩一阵沉默）",
)
_SILENCE_LONG = (
    "（沉默蔓延开来，时间像是凝住了）",
    "（已经很长时间没人说话了）",
    "（安静得几乎能听见自己的呼吸）",
)
_SILENCE_DEFAULT = (
    "（安静了片刻）",
    "（四周静了下来）",
    "（一时没有人说话）",
)
""" 冷场环境描述（零 token，按「悬著问题 / 长时间沉默 / 普通」三档确定性地选一条）"""


def describe_silence(recent_texts: list[str], idle_seconds: float = 0.0) -> str:
    """生成一段在「冷场 / 悬而未决」时抛给角色的自然环境描述（零 token，规则驱动）。

    Args:
        recent_texts: 最近对话文本（旧 -> 新）
        idle_seconds: 距上次互动的空闲秒数

    Returns:
        str: 一条自然的环境描述（作为快照 content，引导角色主动开口）
    """
    last = recent_texts[-1] if recent_texts else ""
    if "?" in last or "？" in last:
        bucket = _SILENCE_OPEN_QUESTION
    elif idle_seconds >= _LONG_SILENCE_SECONDS:
        bucket = _SILENCE_LONG
    else:
        bucket = _SILENCE_DEFAULT
    # 用内容 CRC 确定性选一条（避免同一轮每次都变，测试稳定）
    idx = zlib.crc32("\n".join(recent_texts).encode("utf-8")) % len(bucket)
    return bucket[idx]


def evaluate_initiative(
    recent_texts: list[str],
    *,
    character_name: str,
    weights: dict[str, float] | None = None,
    idle_seconds: float = 0.0,
    expression_base: float = 0.5,
) -> float:
    """评估当前是否值得主动开口。

    Args:
        recent_texts: 最近对话文本（旧 -> 新）
        character_name: 角色名（用于关系牵引判定）
        weights: 四维权重，缺省等权；来自角色卡 motivation_weights
        idle_seconds: 距上次互动的空闲秒数
        expression_base: 角色表达欲基线（话痨度），来自角色卡 expression_base

    Returns:
        float: 对话欲 0.0 ~ 1.0，越高越该主动发言
    """
    merged = _DEFAULT_WEIGHTS | (weights or {})
    total_w = sum(merged.values()) or 1.0

    texts = "\n".join(recent_texts)
    last = recent_texts[-1] if recent_texts else ""

    dims = {
        # 表达欲：角色天性基线（话痨 vs 沉默寡言），靠角色卡 expression_base 区分
        "expression": min(1.0, max(0.0, expression_base)),
        # 情绪唤起：每命中 1 个情绪词 +0.25，4 个封顶
        "emotional": min(1.0, sum(word in texts for word in _EMOTION_WORDS) / 4),
        # 关系牵引：被点名直接满值，被 @ 也有牵动
        "relational": (
            1.0 if character_name and character_name in texts else (0.3 if "@" in texts else 0.0)
        ),
        # 情境时机：空闲越长越该冒泡（1 小时饱和）+ 悬而未决的问题
        "situational": min(1.0, idle_seconds / 3600.0) * 0.6
        + (0.4 if ("?" in last or "？" in last) else 0.0),
    }
    return min(1.0, sum(merged[k] * v for k, v in dims.items()) / total_w)

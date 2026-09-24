"""文风防复读 —— 治小模型「重复自己的模板/口头禅」。

两个病灶来自 20 轮真机实录（logs/edge-2.jsonl，temp/report_edge.py 检出）：
- 相邻两轮回复相似度 88%（输入只换俩字，输出改仨字——骨架逐字复用）；
- 同一收尾梗（「来，张嘴——啊～」）连用 6 次（人设一致性满分，多样性零分）。

两道防线，全在 Responder 出口侧：
1. **预防性提示**（流式/缓冲路径都生效）：生成前把「你刚才怎么说的、哪些收尾
   用腻了」塞进 responder.user 的 [自我克制] 段——零额外模型调用；
2. **兜底重写/剥结尾**（缓冲路径）：相似度超阈值触发一次纠偏重写；同一收尾
   在近期窗口用够次数就剥掉（ported qqbot「不复读结尾」规则，确定性零 token）。
"""

import difflib
import re
from collections import Counter
from collections.abc import Iterable, Sequence

__all__ = [
    "avoid_hint",
    "ending_key",
    "should_retry",
    "should_strip_ending",
    "similarity",
    "split_sentences",
    "strip_ending",
]

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?…~～\n])")
""" 句读边界（切句保留标点在句尾）"""

_NORMALIZE = re.compile(r"[\s,，.。~～…、!！?？;；:：—_\-—()（）「」『』\"'“”‘’]+")
""" 归一化要抹掉的标点/空白（收尾指纹比对用）"""

_MIN_KEY_LEN = 3
""" 收尾指纹最小长度：短于它的（「哦」「嗯」）是语气词不是梗，不治 """

PARROT_REASON = (
    "与你上一句的结构、开头、结尾过于相似（在复读自己的模板）。"
    "换一个开头、换一个收尾方式、换一个比喻重写；保持角色，但不要重复自己。"
)
""" 防复读纠偏重写的原因文本（ responder.correct 的 reason） """


def split_sentences(text: str) -> list[str]:
    """按句读标点/换行切句（保留标点在句尾，去空段）。"""
    return [part for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]


def _normalize(text: str) -> str:
    return _NORMALIZE.sub("", text)


def ending_key(reply: str) -> str:
    """回复的收尾指纹：最后一句的归一化形式（治「同一收尾反复用」的比对键）。

    Args:
        reply: 完整回复文本

    Returns:
        str: 归一化收尾键；太短（语气词）或无句子时返回空串（不参与去重）
    """
    sentences = split_sentences(reply)
    if not sentences:
        return ""
    key = _normalize(sentences[-1])
    return key if len(key) >= _MIN_KEY_LEN else ""


def strip_ending(reply: str) -> str:
    """剥掉回复的最后一句（收尾去重用）。

    只有一句的回复不剥（剥了就空了），原样返回。

    Args:
        reply: 完整回复文本

    Returns:
        str: 剥掉尾句的文本（或原样）
    """
    sentences = split_sentences(reply)
    if len(sentences) < 2:
        return reply
    return "".join(sentences[:-1]).rstrip()


def similarity(a: str, b: str) -> float:
    """两条文本的相似度（0~1，字符级 SequenceMatcher）。

    实录标定：骨架复用 88%、正常 callback 复用 <40%——默认阈值取 0.75。
    """
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def avoid_hint(prev_reply: str, recent_endings: Iterable[str]) -> str:
    """生成「自我克制」提示文本（responder.user 的 avoid 段）。

    Args:
        prev_reply: 上一轮回复（提示避开它的开头/结尾/结构）
        recent_endings: 近期收尾指纹窗口（出现 ≥2 次的点名别再用）

    Returns:
        str: 提示文本；无需克制时返回空串（不注入）
    """
    parts: list[str] = []
    if prev_reply and prev_reply.strip():
        head = prev_reply.strip()[:20]
        tail = prev_reply.strip()[-16:]
        parts.append(
            f"你刚才说过：「{head}……{tail}」。"
            f"这句不要重复同样的开头、结尾和整体结构，换一种切入方式和收尾方式。"
        )
    counts = Counter(k for k in recent_endings if k)
    repeats = [k for k, count in counts.items() if count >= 2 and len(k) >= 2]
    if repeats:
        joined = "」「".join(sorted(repeats)[:2])
        parts.append(f"你的收尾「{joined}」最近已反复出现，这句换个结尾。")
    return "\n".join(parts)


def should_retry(reply: str, prev_reply: str, threshold: float) -> bool:
    """是否触发防复读重写（相似度超阈值且阈值开启）。

    Args:
        reply: 本轮新生成的回复
        prev_reply: 上一轮回复
        threshold: 相似度阈值；<= 0 表示关闭

    Returns:
        bool: True 表示该纠偏重写
    """
    if threshold <= 0 or not prev_reply:
        return False
    return similarity(reply, prev_reply) >= threshold


def should_strip_ending(reply: str, recent_endings: Sequence[str]) -> bool:
    """是否剥掉本轮收尾（同一收尾指纹在近期窗口已出现 ≥2 次）。"""
    key = ending_key(reply)
    if not key:
        return False
    return sum(1 for k in recent_endings if k == key) >= 2

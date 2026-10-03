"""ParrotGuard 组件测试 —— 防复读守门（相似度纠偏 / 收尾去重 / 状态回填）。

构造组件只需要 StyleSettings + 一个假 Responder（correct 按脚本回话），
不再组装整个世界——窗口状态与纠偏行为在这里直接可测。
"""

from gensokyoai.core.config import StyleSettings
from gensokyoai.roleplay.components.parrot import ParrotGuard


class _StubResponder:
    """只实现 correct() 的假表达层：按脚本回话并记录每次纠偏原因。"""

    def __init__(self, rewrite: str) -> None:
        self.rewrite = rewrite
        self.calls: list[str] = []

    async def correct(self, bad_reply: str, reason: str) -> str:
        self.calls.append(reason)
        return self.rewrite


def _guard(rewrite: str = "换个说法。完全不一样的桥段哦。", **style) -> ParrotGuard:
    """组一个防复读守门（style 缺省走默认阈值）。"""
    return ParrotGuard(StyleSettings(**style), _StubResponder(rewrite))


def test_guard_starts_empty():
    """初始窗口：无上一轮回复、收尾窗口按 StyleSettings 定尺"""
    guard = _guard()
    assert guard._last_reply == ""
    assert guard._recent_endings.maxlen == 6


async def test_guard_rewrites_on_high_similarity():
    """相似度超阈值 -> 花一次纠偏重写；重写版更不相似则采用"""
    guard = _guard()
    same = "开头一。结尾二。桥段三。比喻四。"
    guard.note_reply(same)
    kept = await guard.guard(same)
    assert len(guard._responder.calls) == 1
    assert "复读" in guard._responder.calls[0]
    assert kept == "换个说法。完全不一样的桥段哦。"


async def test_guard_disabled_by_threshold_zero():
    """similarity_retry=0 关闭守门：完全重合也不重写"""
    guard = _guard("别的说法", similarity_retry=0.0)
    same = "一模一样的回复"
    guard.note_reply(same)
    assert await guard.guard(same) == same
    assert guard._responder.calls == []


def test_dedup_ending_strips_repeated_ending():
    """同一收尾近期窗口出现 ≥2 次 -> 剥掉"""
    guard = _guard()
    reply = "这次换了说法。来，张嘴——啊～"
    guard._recent_endings.extend(["来张嘴啊", "来张嘴啊"])
    assert guard.dedup_ending(reply) == "这次换了说法。"


def test_dedup_ending_keeps_fresh_ending():
    """收尾没重复过 -> 原样保留"""
    guard = _guard()
    reply = "这次换了说法。来，张嘴——啊～"
    guard._recent_endings.extend(["呵呵", "嗯嗯"])
    assert guard.dedup_ending(reply) == reply


def test_note_reply_records_ending():
    """note_reply 同时更新比对基准与收尾指纹窗口"""
    guard = _guard()
    guard.note_reply("说了一句话。来，张嘴——啊～")
    assert guard._last_reply.endswith("来，张嘴——啊～")
    assert list(guard._recent_endings) == ["来张嘴啊"]


async def test_guard_catches_skip_turn_repeat():
    """隔一轮原句复读（A→B→A）也能照出来：比对基准是窗口不是相邻轮"""
    guard = _guard("换个完全不一样的说法哦。")
    reply_a = "……喜欢？那种东西，早就被纯化掉了。"
    guard._recent_replies.append(reply_a)
    guard._last_reply = "……不必。你并无亏欠，只是问错了对象。"  # 相邻轮是另一条
    kept = await guard.guard(reply_a)
    assert len(guard._responder.calls) == 1, "隔轮复读应触发重写"
    assert kept == "换个完全不一样的说法哦。"


async def test_guard_passes_when_different_from_window():
    """与窗口内各轮都不相似 -> 不重写"""
    guard = _guard("不应被用到")
    guard._recent_replies.extend(["苹果好吃。", "今天天气不错。"])
    guard._last_reply = "今天天气不错。"
    kept = await guard.guard("月之都的闹剧何时休。")
    assert kept == "月之都的闹剧何时休。"
    assert guard._responder.calls == []


def test_note_reply_fills_window():
    """回复落进近期窗口（供后续轮次比对）"""
    guard = _guard()
    guard.note_reply("第一句。")
    guard.note_reply("第二句。")
    assert list(guard._recent_replies) == ["第一句。", "第二句。"]
    assert guard._recent_replies.maxlen == 3

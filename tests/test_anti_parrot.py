"""文风防复读单元测试 —— 收尾指纹 / 剥结尾 / 相似度 / 预防提示的纯函数部分。

有状态的守门行为见 test_parrot.py（ParrotGuard 组件）；疑似注入短回复的
标记测试已随 OOCGuard 拆分迁至 test_ooc_guard.py。
"""

from gensokyoai.core.responder.anti_parrot import (
    avoid_hint,
    ending_key,
    should_retry,
    should_strip_ending,
    similarity,
    strip_ending,
)

# ---------- 纯函数 ----------


def test_ending_key_normalizes_last_sentence():
    """收尾指纹 = 最后一句归一化（抹标点）"""
    assert ending_key("……来，张嘴——啊～") == "来张嘴啊"
    assert ending_key("……呵呵～") == ""


def test_ending_key_min_length():
    """短于 3 字的是语气词不是梗，不参与去重"""
    assert ending_key("……哦。") == ""
    assert ending_key("好的呢？") == "好的呢"  # 恰好 3 字，算


def test_strip_ending_keeps_single_sentence():
    """一句话的回复不剥（剥了就空了）"""
    assert strip_ending("只有一句哦。") == "只有一句哦。"


def test_strip_ending_removes_last_sentence():
    assert strip_ending("第一句。第二句。") == "第一句。"


def test_similarity_identical_and_different():
    assert similarity("你好呀", "你好呀") == 1.0
    assert similarity("今晚的月色真美", "团子真好吃") < 0.2


def test_should_retry_threshold():
    a = "完全一样的回复内容哈哈哈"
    assert should_retry(a, a, 0.75) is True
    assert should_retry(a, a, 0.0) is False, "阈值 0 = 关闭"
    assert should_retry("完全无关的另一句话", a, 0.75) is False


def test_should_strip_ending_after_two_uses():
    assert should_strip_ending("来，张嘴——啊～", ["来张嘴啊", "来张嘴啊"]) is True
    assert should_strip_ending("来，张嘴——啊～", ["来张嘴啊"]) is False


def test_avoid_hint_empty_without_history():
    assert avoid_hint("", []) == ""


def test_avoid_hint_mentions_prev_reply_and_repeat_ending():
    hint = avoid_hint("来，张嘴——啊～", ["来张嘴啊", "来张嘴啊"])
    assert "张嘴" in hint
    assert "不要重复" in hint


def test_avoid_hint_mentions_recent_replies_window():
    """窗口模式点名最近两条回复（隔轮复读也能点到名）"""
    hint = avoid_hint(
        "",
        [],
        ["第一句开头。第一句结尾。", "第二句开头。第二句结尾。", "第三句开头。第三句结尾。"],
    )
    assert "第二句开头" in hint and "第三句开头" in hint
    assert "第一句开头" not in hint, "最多展示最近两条，防提示膨胀"
    assert "不要重复" in hint

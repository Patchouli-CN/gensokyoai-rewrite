"""主动发言评估单元测试：四维对话欲规则"""

from gensokyoai.roleplay.initiative import evaluate_initiative


def test_idle_silence_has_low_urge():
    """无情绪、无点名、无空闲时对话欲很低"""
    urge = evaluate_initiative(
        ["小明: 今天天气不错", "小红: 是啊"], character_name="幽幽子", idle_seconds=0.0,
    )
    assert urge < 0.3


def test_name_mention_raises_urge():
    """被点名大幅提升对话欲（关系牵引满值）"""
    base = evaluate_initiative(["小明: 大家好"], character_name="幽幽子")
    named = evaluate_initiative(["小明: 幽幽子在吗？"], character_name="幽幽子")
    assert named > base
    assert named > 0.4


def test_pending_question_raises_urge():
    """最后一条消息带问号提升情境时机"""
    base = evaluate_initiative(["小明: 今天吃了吗"], character_name="幽幽子")
    asked = evaluate_initiative(["小明: 有人知道吗？"], character_name="幽幽子")
    assert asked > base


def test_idle_time_scales_situational():
    """空闲越久对话欲越高（1 小时饱和）"""
    short = evaluate_initiative(["小明: 嗯"], character_name="幽幽子", idle_seconds=60)
    long = evaluate_initiative(["小明: 嗯"], character_name="幽幽子", idle_seconds=7200)
    assert long > short


def test_weights_change_character_voice():
    """motivation_weights 区分话痨与沉默角色"""
    texts = ["小明: 聊聊?", "小红: 好呀，真开心！"]
    talkative = evaluate_initiative(
        texts, character_name="魔理沙",
        weights={"expression": 1.0, "emotional": 0.0, "relational": 0.0, "situational": 0.0},
    )
    quiet = evaluate_initiative(
        texts, character_name="魔理沙",
        weights={"expression": 0.0, "emotional": 1.0, "relational": 0.0, "situational": 0.0},
    )
    assert talkative > quiet


def test_urge_bounded_and_safe_on_empty():
    """空输入安全，输出始终在 0~1"""
    urge = evaluate_initiative([], character_name="幽幽子", idle_seconds=99999)
    assert 0.0 <= urge <= 1.0

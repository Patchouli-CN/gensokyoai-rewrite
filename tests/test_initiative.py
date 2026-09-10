"""主动发言评估单元测试：四维对话欲规则"""

from gensokyoai.roleplay.initiative import describe_silence, evaluate_initiative


def test_idle_silence_has_low_urge():
    """无情绪、无点名、无空闲时对话欲很低"""
    urge = evaluate_initiative(
        ["小明: 今天天气不错", "小红: 是啊"],
        character_name="幽幽子",
        idle_seconds=0.0,
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
        texts,
        character_name="魔理沙",
        weights={"expression": 1.0, "emotional": 0.0, "relational": 0.0, "situational": 0.0},
    )
    quiet = evaluate_initiative(
        texts,
        character_name="魔理沙",
        weights={"expression": 0.0, "emotional": 1.0, "relational": 0.0, "situational": 0.0},
    )
    assert talkative > quiet


def test_urge_bounded_and_safe_on_empty():
    """空输入安全，输出始终在 0~1"""
    urge = evaluate_initiative([], character_name="幽幽子", idle_seconds=99999)
    assert 0.0 <= urge <= 1.0


def test_expression_base_differentiates_characters():
    """expression_base 区分话痨与沉默角色（表达欲基线，不再写死 0.5）"""
    texts = ["小明: 嗯", "小红: 哦"]
    chatty = evaluate_initiative(texts, character_name="幽幽子", expression_base=0.8)
    quiet = evaluate_initiative(texts, character_name="幽幽子", expression_base=0.2)
    assert chatty > quiet


def test_describe_silence_buckets_and_deterministic():
    """冷场描述按「悬着问题 / 长时间沉默 / 普通」分档，且确定性稳定"""
    q = describe_silence(["小明: 有人知道吗？"], idle_seconds=10.0)
    long = describe_silence(["小明: 嗯"], idle_seconds=7200)
    normal = describe_silence(["小明: 吃饭了"], idle_seconds=30.0)

    assert "悬在半空" in q or "没人接话" in q or "走神" in q
    assert "很久" in long or "凝住" in long or "呼吸" in long
    assert normal in ("（安静了片刻）", "（四周静了下来）", "（一时没有人说话）")
    # 同一输入确定性同一条
    assert describe_silence(["小明: 有人知道吗？"], idle_seconds=10.0) == q

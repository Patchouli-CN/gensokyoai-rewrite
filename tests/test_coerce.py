"""utils/coerce 单元测试：模型 JSON 字段的宽松取值"""

from gensokyoai.utils.coerce import as_bool, as_float, as_str, clamp01


def test_as_float_rejects_null_strings_and_bools():
    """null / 字符串 / 布尔回落默认值，数字原样转浮点"""
    assert as_float(None, 0.5) == 0.5
    assert as_float("high", 0.5) == 0.5
    assert as_float(True, 0.5) == 0.5
    assert as_float(3, 0.5) == 3.0


def test_clamp01_bounds_and_defaults():
    """越界夹回 0~1，非法值回落默认值"""
    assert clamp01(1.7) == 1.0
    assert clamp01(-2) == 0.0
    assert clamp01(None, 0.3) == 0.3


def test_as_str_never_yields_none_text():
    """None 不会变成字符串 "None"；容器回落默认值，数字转文本"""
    assert as_str(None) == ""
    assert as_str(None, "旧值") == "旧值"
    assert as_str({"a": 1}) == ""
    assert as_str(42) == "42"
    assert as_str("你好") == "你好"


def test_as_bool_reads_string_booleans():
    """ "false" 字符串是 False（裸 bool("false") 是 True）"""
    assert as_bool("false") is False
    assert as_bool(" TRUE ") is True
    assert as_bool(True) is True
    assert as_bool(None) is False
    assert as_bool("maybe", default=True) is True

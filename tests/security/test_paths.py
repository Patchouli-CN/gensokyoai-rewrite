"""房间名 -> 目录名：安全字符原样，危险字符转义。"""

from gensokyoai.paths import storage_key


def test_safe_names_are_untouched():
    """常见房间名不该被改（已有目录不搬家）"""
    for name in ("g1093789154", "u364292616", "room-1", "a_b.c"):
        assert storage_key(name) == name


def test_windows_illegal_chars_are_escaped():
    """冒号、斜杠这些在 Windows 上不能当目录名"""
    key = storage_key("discord:1234/5678")

    assert ":" not in key and "/" not in key
    assert key.startswith("discord%3A")


def test_unicode_is_kept():
    """中文与 emoji 本身没问题，别乱转"""
    assert storage_key("群 测试🎉") == "群 测试🎉"


def test_traversal_cannot_escape():
    """`.`/`..` 开头不能跑出数据目录"""
    for name in ("..", "../escape", ".hidden"):
        key = storage_key(name)
        assert not key.startswith("."), key


def test_reserved_windows_names():
    """CON/PRN 这些在 Windows 上不能当文件名"""
    assert storage_key("con") != "con"


def test_empty_name_has_placeholder():
    assert storage_key("") == "_"
    assert storage_key("   ") == "_"


def test_long_names_are_truncated_with_hash():
    """太长的名字截断并加哈希，避免超长与重名"""
    long_name = "很长的房间名" * 40
    key = storage_key(long_name)

    assert len(key) <= 96 + 9
    assert key != storage_key(long_name + "x")
    assert storage_key(long_name) == storage_key(long_name)

"""房间名到路径的映射。

房间名可以任意（`g123` / `discord:1234` / 中文也行），但落盘时要当目录名用。
这里只转义"当路径会出事"的那部分：Windows 非法字符、控制字符、以及 `.`/`..`。

安全名字原样返回，所以已有目录不会搬家。
"""

_ILLEGAL = set('<>:"/\\|?*')
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_MAX = 96


def _escape(char: str) -> str:
    return "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))


def storage_key(name: str) -> str:
    """房间名 -> 目录名；只转义当路径会出问题的字符。

    Args:
        name: 任意房间名

    Returns:
        str: 能安全当目录名的字符串（空名给 `_`）
    """
    text = (name or "").strip() or "_"
    parts = []
    for char in text:
        if char in _ILLEGAL or ord(char) < 32:
            parts.append(_escape(char))
        else:
            parts.append(char)
    key = "".join(parts)
    if key.startswith("."):
        key = _escape(key[0]) + key[1:]
    if key.split(".")[0].lower() in _RESERVED:
        key = _escape(key[0]) + key[1:]
    key = key.rstrip(" .") or "_"
    if len(key) > _MAX:
        key = key[:_MAX] + "-" + _hash(text)
    return key


def _hash(text: str) -> str:
    """短哈希（截断重名用）。"""
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

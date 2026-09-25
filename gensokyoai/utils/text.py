"""文本处理工具"""

import re

import msgspec

_CHANNEL_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_safe_channel_id(channel_id: str) -> bool:
    """频道名是否安全——它会直接拼进落盘路径，必须不含任何路径语义字符。

    只允许字母 / 数字 / 下划线 / 连字符，1~64 字符；`..`、斜杠、反斜杠、
    百分号编码、Unicode 同形字一律拒绝。

    Args:
        channel_id: 待校验频道名

    Returns:
        bool: 是否可安全用作频道 / 会话标识
    """
    return bool(_CHANNEL_ID_PATTERN.fullmatch(channel_id))


def clean_whitespace(text: str) -> str:
    """
    清洗多余空白字符。

    - 将连续空白（含换行、制表符）压缩为单个空格
    - 去除首尾空白

    Args:
        text: 原始文本

    Returns:
        清洗后的文本
    """
    return re.sub(r"\s+", " ", text).strip()


def strip_control_chars(text: str) -> str:
    """
    去除控制字符（保留换行和制表符）。

    Args:
        text: 原始文本

    Returns:
        去除控制字符后的文本
    """
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def truncate(text: str, max_length: int, suffix: str = "...") -> str:
    """
    按字符数截断文本，超长时追加后缀。

    Args:
        text: 原始文本
        max_length: 最大字符数（含后缀）
        suffix: 截断后缀，默认 '...'

    Returns:
        截断后的文本
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - len(suffix)] + suffix


def safe_truncate(text: str, max_length: int, suffix: str = "...") -> str:
    """
    安全截断：尽量不在词/字中间切断。

    优先在标点或空格处截断，找不到则在字符边界截断。

    Args:
        text: 原始文本
        max_length: 最大字符数（含后缀）
        suffix: 截断后缀，默认 '...'

    Returns:
        截断后的文本
    """
    if len(text) <= max_length:
        return text

    cut_at = max_length - len(suffix)
    if cut_at <= 0:
        return suffix[:max_length]

    # 在截断位置往前找最近的断点（标点或空格）
    break_chars = "，。！？、；：,.!?;: \n"
    for i in range(cut_at, max(cut_at // 2, 0), -1):
        if text[i - 1] in break_chars:
            return text[:i] + suffix

    return text[:cut_at] + suffix


def count_chars(text: str) -> dict[str, int]:
    """
    统计文本中的字符分布。

    Args:
        text: 原始文本

    Returns:
        包含 chinese、ascii、other 三类字符数的字典
    """
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    ascii_count = sum(1 for c in text if ord(c) < 128)
    other = len(text) - chinese - ascii_count
    return {"chinese": chinese, "ascii": ascii_count, "other": other}


def extract_json_object(text: str) -> dict:
    """容错提取模型输出中的 JSON 对象（取第一个 { 到最后一个 } 的切片解码）。

    小模型输出常见噪声——思考残留、```json 围栏、首尾废话——通常都落在
    这个窗口之外。失败语义统一为 ValueError，降级策略由调用方决定。

    Args:
        text: 模型原始输出

    Returns:
        解析出的 JSON 对象

    Raises:
        ValueError: 找不到 JSON 对象或解析失败
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"输出不含 JSON 对象: {text[:80]!r}")
    try:
        return msgspec.json.decode(text[start : end + 1], type=dict)
    except msgspec.DecodeError as err:
        raise ValueError(f"JSON 解析失败: {text[:80]!r}") from err


def try_extract_json_object(text: str) -> dict | None:
    """extract_json_object 的宽容版：失败返回 None（调用方自行降级）。

    Args:
        text: 模型原始输出

    Returns:
        解析出的 JSON 对象；失败为 None
    """
    try:
        return extract_json_object(text)
    except ValueError:
        return None

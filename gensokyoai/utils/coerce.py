"""模型 JSON 字段的宽松取值 —— 小模型写 `null` / `"high"` / `"false"` 是常态。

裸 `float()` / `str()` / `bool()` 在这些值上要么抛异常（一路冒到主循环），
要么悄悄算错（`str(None)` 得到 "None"、`bool("false")` 得到 True）。
所有解析模型输出的地方统一走这里：类型对得上才用，否则回落默认值。
"""


def as_float(value: object, default: float) -> float:
    """取浮点；布尔 / 非数值（含 None、字符串）回落默认值。

    Args:
        value: 模型给的原始值
        default: 回落值

    Returns:
        float: 数值本身或默认值
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def clamp01(value: object, default: float = 0.0) -> float:
    """取 0~1 的概率；非法值回落默认值，越界值夹回区间。

    Args:
        value: 模型给的原始值
        default: 回落值

    Returns:
        float: 0~1 之间的概率
    """
    return min(1.0, max(0.0, as_float(value, default)))


def as_str(value: object, default: str = "") -> str:
    """取字符串；None / 容器类型回落默认值，数字转成文本。

    Args:
        value: 模型给的原始值
        default: 回落值

    Returns:
        str: 文本本身、数字的文本形式或默认值
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def as_bool(value: object, default: bool = False) -> bool:
    """取布尔；接受真布尔与 "true"/"false" 字符串，其余回落默认值。

    Args:
        value: 模型给的原始值
        default: 回落值

    Returns:
        bool: 布尔值
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
    return default

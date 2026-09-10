"""Token 计数工具"""

from typing import Protocol


class TokenCounter(Protocol):
    """Token 计数器协议"""

    def count(self, text: str) -> int:
        """计算文本的 token 数量"""
        ...


class DefaultCounter:
    """
    默认token计数器
    """

    def count(self, text: str) -> int:
        chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - chinese
        return int(chinese * 1.5 + other * 0.25)

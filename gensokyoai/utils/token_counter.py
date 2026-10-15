"""Token 计数工具"""

import re
from functools import lru_cache
from typing import Protocol

_CJK = re.compile(r"[一-鿿]")


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
        """\u4f30\u7b97 token \u6570\uff08\u4e2d\u6587 1.5 / \u5176\u4ed6 0.25\uff09\u3002

        \u6bcf\u6b21\u6a21\u578b\u8c03\u7528\u90fd\u8981\u5bf9\u6574\u6bb5\u4f1a\u8bdd\u5386\u53f2\u6570 2~3 \u904d\uff08\u88c1\u526a / \u65e5\u5fd7\u5360\u7528 / \u5065\u5eb7\u5360\u7528\u7387\uff09\uff0c
        \u800c\u5386\u53f2\u91cc\u7edd\u5927\u591a\u6570\u6d88\u606f\u662f\u4e0a\u4e00\u8f6e\u539f\u6837\u7559\u4e0b\u7684\u2014\u2014\u6309\u6587\u672c\u7f13\u5b58\uff0c\u53ea\u6570\u65b0\u6d88\u606f\u3002

        Args:
            text: \u5f85\u8ba1\u6570\u6587\u672c

        Returns:
            int: \u4f30\u7b97\u7684 token \u6570
        """
        return _estimate(text)


@lru_cache(maxsize=4096)
def _estimate(text: str) -> int:
    chinese = len(_CJK.findall(text))
    other = len(text) - chinese
    return int(chinese * 1.5 + other * 0.25)

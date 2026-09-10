"""口层 —— 输出投递层（与 eyes 输入感知层对称）。

隐喻：眼睛看（eyes 输入）→ 脑子想（brain 决策）→ 嘴巴说（mouth 输出）。
平台差异（控制台 / QQ 群聊 / ...）被 mouth 屏蔽成统一的投递接口。
"""

from .base import Mouth
from .console import ConsoleMouth

__all__ = [
    "Mouth",
    "ConsoleMouth",
]

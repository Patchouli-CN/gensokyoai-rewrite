"""命令子模块（斜杠指令的注册与执行）。"""

from .core import Command, CommandContext, CommandExecutor, Permission, permission_of

__all__ = [
    "Command",
    "CommandContext",
    "CommandExecutor",
    "Permission",
    "permission_of",
]

"""命令执行结果"""

from enum import Enum, auto
from typing import Any

from msgspec import Struct


class CommandStatus(Enum):
    """命令执行状态"""

    SUCCESS = auto()
    FAILURE = auto()
    NO_HANDLER = auto()


class CommandResult(Struct, frozen=False):
    """命令执行结果"""

    command: str
    status: CommandStatus
    message: str = ""
    data: Any = None
    should_exit: bool = False

    @classmethod
    def success(cls, command: str, message: str = "", data: Any = None) -> CommandResult:
        """构造成功结果"""
        return cls(command=command, status=CommandStatus.SUCCESS, message=message, data=data)

    @classmethod
    def failure(cls, command: str, message: str) -> CommandResult:
        """构造失败结果"""
        return cls(command=command, status=CommandStatus.FAILURE, message=message)

    @classmethod
    def no_handler(cls, command: str) -> CommandResult:
        """构造「无 handler」结果（命令未注册）"""
        return cls(command=command, status=CommandStatus.NO_HANDLER, message=f"未知命令: {command}")

    @classmethod
    def exit(cls, message: str = "程序正在退出") -> CommandResult:
        """构造退出结果（should_exit=True，执行器收到后停止处理后续命令）"""
        return cls(command="exit", status=CommandStatus.SUCCESS, message=message, should_exit=True)

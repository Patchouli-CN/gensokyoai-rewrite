"""命令执行上下文"""

from typing import TypeVar

from msgspec import Struct

from .permission import PermissionLevel

T = TypeVar("T")


class CommandContext(Struct, frozen=False):
    """命令执行上下文"""

    source: str = "console"
    """ 来源标识 """
    issuer: str = "Console"
    """ 发起者 """
    metadata: dict = {}
    """ 附加元数据 """
    permission: PermissionLevel = PermissionLevel.OWNER
    """ 调用方权限级别 """

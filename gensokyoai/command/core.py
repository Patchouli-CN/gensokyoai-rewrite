"""命令子模块 —— 注册表 + 权限 + 执行器（斜杠指令的统一入口）。

设计约束：
- 命令不进房间、不调模型（本地执行，回系统帧）
- 未知命令与权限不足一律静默——不暴露命令面
- 权限三级：visitor < admin < owner（角色由可信接入侧声明，与风格同一边界）
"""

from collections.abc import Awaitable, Callable
from typing import Literal

import msgspec

type Permission = Literal["visitor", "admin", "owner"]
""" 权限级别（升序） """

_LEVEL = {"visitor": 0, "admin": 1, "owner": 2}

type CommandHandler = Callable[["CommandContext", str], Awaitable[str]]
""" 命令处理器：(上下文, 参数文本) -> 回复文本 """


class CommandContext(msgspec.Struct, frozen=True):
    """一次命令执行的上下文。"""

    issuer: str = ""
    """ 发起者 """
    permission: Permission = "visitor"
    """ 发起者权限 """
    room: str = ""
    """ 发起时所在房间 """


class Command(msgspec.Struct, frozen=True):
    """一条命令。"""

    name: str
    handler: CommandHandler
    permission: Permission = "visitor"
    aliases: tuple[str, ...] = ()
    desc: str = ""


class CommandExecutor:
    """命令执行器：解析 -> 查表 -> 权限校验 -> 执行。"""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}
        """ name -> Command（/help 用） """
        self._handlers: dict[str, Command] = {}
        """ name/alias -> Command（分派用） """

    def register(self, command: Command) -> None:
        """注册一条命令（别名一并登记）。"""
        if command.name in self._handlers:
            raise ValueError(f"命令重复注册: {command.name}")
        self._commands[command.name] = command
        self._handlers[command.name] = command
        for alias in command.aliases:
            if alias in self._handlers:
                raise ValueError(f"命令别名冲突: {alias}")
            self._handlers[alias] = command

    def visible(self, permission: Permission) -> list[Command]:
        """某权限可见的命令列表（/help 用）。"""
        return [
            command
            for command in self._commands.values()
            if _LEVEL[command.permission] <= _LEVEL[permission]
        ]

    async def execute(self, text: str, ctx: CommandContext) -> str | None:
        """执行一条输入；不是命令 / 未知 / 权限不足都返回 None（静默）。

        Args:
            text: 原始输入（"/doctor 参数"）
            ctx: 执行上下文

        Returns:
            str | None: 回复文本；None = 调用方当普通消息处理或静默吞掉
        """
        if not text.startswith("/"):
            return None
        name, _, args = text[1:].partition(" ")
        command = self._handlers.get(name)
        if command is None:
            return None
        if _LEVEL[command.permission] > _LEVEL[ctx.permission]:
            return None
        return await command.handler(ctx, args.strip())


def permission_of(role: str) -> Permission:
    """接入侧声明的角色映射到权限（owner/admin 之外一律 visitor）。"""
    return role if role in _LEVEL else "visitor"  # type: ignore[return-value]

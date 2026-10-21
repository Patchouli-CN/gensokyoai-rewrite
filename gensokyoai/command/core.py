"""命令子模块 —— 注册表 + 权限 + 执行器（斜杠指令的统一入口）。

设计约束：
- 命令不进房间、不调模型（本地执行，回系统帧）
- 未知命令与权限不足一律静默——不暴露命令面
- 权限三级：visitor < admin < owner（角色由可信接入侧声明，与风格同一边界）
"""

import time
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
    coalesce_s: float = 0.0
    """ 同一个房间里这段时间内只执行一次（回复大家都能看见，重复查没意义） """


class CommandExecutor:
    """命令执行器：解析 -> 查表 -> 权限校验 -> 合并重复 -> 执行。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """初始化。

        Args:
            clock: 时间源（合并窗口用；测试注入）
        """
        self._commands: dict[str, Command] = {}
        """ name -> Command（/help 用） """
        self._handlers: dict[str, Command] = {}
        """ name/alias -> Command（分派用） """
        self._clock = clock
        self._seen: dict[tuple[str, str, str], float] = {}
        """ (房间, 命令名, 参数) -> 上次执行时间 """

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
        """执行一条输入；不是命令 / 未知 / 权限不足 / 窗口内重复都返回 None（静默）。

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
        if self._is_duplicate(command, args.strip(), ctx.room):
            return None
        return await command.handler(ctx, args.strip())

    def _is_duplicate(self, command: Command, args: str, room: str) -> bool:
        """同一个房间里，这条命令是不是刚执行过（窗口由命令自己声明）。

        先记账再执行：A 的查询还在路上时 B 又来，B 也该被吞掉。
        """
        if command.coalesce_s <= 0:
            return False
        now = self._clock()
        key = (room, command.name, args)
        last = self._seen.get(key)
        if last is not None and now - last < command.coalesce_s:
            return True
        self._seen[key] = now
        if len(self._seen) > 512:  # 别让它无限长
            cutoff = now - max(
                command.coalesce_s, max(c.coalesce_s for c in self._commands.values())
            )
            self._seen = {
                item: stamp for item, stamp in self._seen.items() if stamp >= cutoff
            }
        return False


def permission_of(role: str) -> Permission:
    """接入侧声明的角色映射到权限（owner/admin 之外一律 visitor）。"""
    return role if role in _LEVEL else "visitor"  # type: ignore[return-value]

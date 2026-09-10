# commands/decorators.py
"""命令装饰器 —— 支持实例级注册表"""

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from .parser import CommandType
from .permission import PermissionLevel


class CommandDefinition:
    """命令定义"""

    def __init__(
        self,
        name: str,
        handler: Callable,
        cmd_type: CommandType = CommandType.CUSTOM,
        aliases: list[str] | None = None,
        description: str = "",
        usage: str = "",
        permission: PermissionLevel = PermissionLevel.OWNER,
    ):
        self.name = name
        self.handler = handler
        self.type = cmd_type
        self.aliases = aliases or []
        self.description = description
        self.permission = permission
        self._sig = inspect.signature(handler)
        self._type_hints = get_type_hints(handler)
        self._is_async = inspect.iscoroutinefunction(handler)
        self.usage = usage or self._generate_usage(handler)

    def _generate_usage(self, handler: Callable) -> str:
        params = []
        for name, param in self._sig.parameters.items():
            if name in ("cmd", "ctx"):
                continue
            if param.default is not inspect.Parameter.empty:
                params.append(f"[{name}={param.default}]")
            else:
                params.append(f"<{name}>")
        return f"/{self.name} " + " ".join(params) if params else f"/{self.name}"

    def parse_args(self, content: str) -> dict:
        args: dict[str, Any] = {}
        param_names = [p for p in self._sig.parameters if p not in ("cmd", "ctx")]

        if not param_names:
            return args

        parts = content.strip().split()

        for i, name in enumerate(param_names):
            if i < len(parts):
                param = self._sig.parameters[name]
                hint = self._type_hints.get(name, str)

                try:
                    if hint is bool:
                        args[name] = parts[i].lower() in ("true", "1", "yes", "on")
                    elif hint is int:
                        args[name] = int(parts[i])
                    elif hint is float:
                        args[name] = float(parts[i])
                    else:
                        args[name] = parts[i]
                except ValueError:
                    args[name] = (
                        param.default if param.default is not inspect.Parameter.empty else None
                    )
            else:
                param = self._sig.parameters[name]
                if param.default is not inspect.Parameter.empty:
                    args[name] = param.default
                else:
                    args[name] = None

        return args

    @property
    def all_names(self) -> list[str]:
        return [self.name] + self.aliases


class CommandRegistry:
    """命令注册表 —— 支持实例级注册（避免全局污染）"""

    def __init__(self):
        self._commands: dict[str, CommandDefinition] = {}

    def register(self, cmd_def: CommandDefinition) -> None:
        for n in cmd_def.all_names:
            self._commands[n.lower()] = cmd_def

    def get(self, name: str) -> CommandDefinition | None:
        return self._commands.get(name.lower())

    def list(self, cmd_type: CommandType | None = None) -> list[CommandDefinition]:
        seen = set()
        result = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                if cmd_type is None or cmd.type == cmd_type:
                    result.append(cmd)
        return result


# 全局默认注册表（兼容旧用法）
_DEFAULT_REGISTRY = CommandRegistry()


def command(
    name: str | None = None,
    cmd_type: CommandType = CommandType.CUSTOM,
    aliases: list[str] | None = None,
    description: str = "",
    usage: str = "",
    permission: PermissionLevel = PermissionLevel.OWNER,
    registry: CommandRegistry | None = None,
):
    """命令装饰器

    Args:
        name: 命令名（默认取函数名去掉 cmd_ 前缀）
        cmd_type: 命令类型
        aliases: 别名列表
        description: 描述
        usage: 用法说明
        permission: 所需权限级别（默认 OWNER）
        registry: 自定义注册表（默认全局注册表）
    """

    def decorator(func: Callable) -> Callable:
        cmd_name = name or func.__name__.replace("cmd_", "")

        cmd_def = CommandDefinition(
            name=cmd_name,
            handler=func,
            cmd_type=cmd_type,
            aliases=aliases,
            description=description,
            usage=usage,
            permission=permission,
        )

        target = registry if registry else _DEFAULT_REGISTRY
        target.register(cmd_def)
        return func

    return decorator


def get_command(name: str) -> CommandDefinition | None:
    return _DEFAULT_REGISTRY.get(name)


def list_commands(cmd_type: CommandType | None = None) -> list[CommandDefinition]:
    return _DEFAULT_REGISTRY.list(cmd_type)

# commands/executor.py
""" 命令执行器 """

from .context import CommandContext
from .decorators import CommandDefinition, CommandRegistry, list_commands
from .parser import CommandParser, CommandType, ParsedCommand
from .result import CommandResult, CommandStatus


class CommandExecutor:
    """ 命令执行器 """

    def __init__(
        self,
        mode: str = "smart",
        registry: CommandRegistry | None = None,
    ):
        self.parser = CommandParser(mode=mode)
        self._registry = registry if registry else CommandRegistry()
        self._sync_parser_tags()

    def _iter_commands(self) -> list[CommandDefinition]:
        return self._registry.list()

    def _get_command(self, name: str) -> CommandDefinition | None:
        return self._registry.get(name)

    def _sync_parser_tags(self) -> None:
        """ 同步命令定义到解析器 """
        for cmd in self._iter_commands():
            self.parser.register_tag(cmd.name, cmd.aliases, cmd.type, cmd.description)
            self.parser.register_prefix(cmd.name, cmd.aliases, cmd.type, cmd.description)

    async def execute(
        self,
        input_text: str,
        context: CommandContext,
    ) -> tuple[list[CommandResult], str]:
        """ 执行命令

        Args:
            input_text: 用户输入文本
            context: 命令上下文

        Returns:
            (执行结果列表, 清理后的纯文本)
        """
        parsed_commands = self.parser.parse(input_text)
        results = []

        for parsed in parsed_commands:
            result = await self._execute_single(parsed, context)
            results.append(result)

            if result.should_exit:
                break

        clean_text = self.parser.extract_clean_text(input_text)
        return results, clean_text

    async def _execute_single(
        self,
        parsed: ParsedCommand,
        context: CommandContext,
    ) -> CommandResult:
        cmd_def = self._get_command(parsed.name)

        if not cmd_def:
            return CommandResult.no_handler(parsed.name)

        # 四级权限闸门
        if context.permission < cmd_def.permission:
            return CommandResult.failure(
                parsed.name,
                f"权限不足：/{parsed.name} 需要 {cmd_def.permission.name} 及以上权限",
            )

        try:
            args = cmd_def.parse_args(parsed.content)
            kwargs = {"ctx": context, "cmd": parsed, **args}
            sig = cmd_def._sig
            filtered_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

            if cmd_def._is_async:
                result = await cmd_def.handler(**filtered_kwargs)
            else:
                result = cmd_def.handler(**filtered_kwargs)

            if not isinstance(result, CommandResult):
                result = CommandResult.success(parsed.name, str(result) if result else "")

            return result

        except Exception as e:
            return CommandResult.failure(parsed.name, str(e))

    def list_commands(self, cmd_type: CommandType | None = None) -> list[CommandDefinition]:
        return self._registry.list(cmd_type)

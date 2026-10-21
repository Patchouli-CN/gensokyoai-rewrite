"""内置命令 —— /doctor（健康中心报告）、/help。"""

from ..health import HealthCenter
from .core import Command, CommandExecutor


def build_executor(health: HealthCenter | None = None) -> CommandExecutor:
    """装配内置命令（健康中心给了才挂 /doctor）。"""
    executor = CommandExecutor()

    if health is not None:

        async def doctor(ctx, args: str) -> str:
            return health.report()

        executor.register(
            Command(
                name="doctor", handler=doctor, aliases=("体检",), desc="运行指标体检"
            )
        )

    async def help_(ctx, args: str) -> str:
        visible = executor.visible(ctx.permission)
        if not visible:
            return "没有可用命令"
        return "可用命令: " + ", ".join(
            f"/{c.name}（{c.desc}）" if c.desc else f"/{c.name}" for c in visible
        )

    executor.register(
        Command(name="help", handler=help_, aliases=("帮助",), desc="列出可用命令")
    )
    return executor

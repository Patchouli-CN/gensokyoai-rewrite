"""内置命令 —— /doctor（健康中心报告）、/balance（查余额）、/help。"""

from ..health import HealthCenter
from ..model import ChatModel
from .core import Command, CommandExecutor

_BALANCE_COALESCE_S = 3.0
""" 同一房间里这段时间内只查一次（回复大家都看得见） """


def build_executor(
    health: HealthCenter | None = None, model: ChatModel | None = None
) -> CommandExecutor:
    """装配内置命令。

    Args:
        health: 健康中心（给了才挂 /doctor）
        model: 模型端点（给了才挂 /balance——要联网查）
    """
    executor = CommandExecutor()

    if health is not None:

        async def doctor(ctx, args: str) -> str:
            return health.report()

        executor.register(
            Command(
                name="doctor", handler=doctor, aliases=("体检",), desc="运行指标体检"
            )
        )

    if model is not None:

        async def balance(ctx, args: str) -> str:
            found = await model.balance()
            if found is None:
                return "这个端点不提供余额查询"
            return f"{found.describe()}（{model.ledger.describe()}）"

        executor.register(
            Command(
                name="balance",
                handler=balance,
                permission="admin",
                aliases=("余额",),
                desc="查剩余额度",
                coalesce_s=_BALANCE_COALESCE_S,
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

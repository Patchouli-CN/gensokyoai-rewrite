"""内置命令 —— /doctor（体检）、/balance（查余额）、/help。"""

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
            if args.strip() in ("raw", "原始"):
                return health.report()
            return _doctor_report(health, model)

        executor.register(
            Command(
                name="doctor",
                handler=doctor,
                aliases=("体检",),
                desc="运行指标体检（/doctor 原始 看内网指标）",
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


def _doctor_report(health: HealthCenter, model: ChatModel | None) -> str:
    """给人看的体检：账户、用量、计费口径、每个群的状况。"""
    lines = [f"运行 {health.uptime / 60:.1f} 分钟"]

    if model is not None:
        lines.extend(_usage_lines(model))

    rooms = _room_lines(health)
    lines.extend(rooms or ["还没有房间活动"])
    return "\n".join(lines)


def _usage_lines(model: ChatModel) -> list[str]:
    """账户 / 用量 / 计费各一行。"""
    ledger = model.ledger
    lines = []
    if model.last_balance is not None:
        lines.append(model.last_balance.describe())

    used = (
        f"用量 {ledger.calls} 次 · 输入 {ledger.prompt_tokens} / 输出 "
        f"{ledger.completion_tokens}"
        f" · 缓存命中 {_cache_rate(ledger):.0%}"
    )
    if ledger.billing is not None:
        used += f" · 约 {_money(ledger)}"
    lines.append(used)

    if ledger.billing is not None:
        lines.append(f"计费 {ledger.describe()}")
    if ledger.quotas:
        lines.append("额度 " + "；".join(ledger.status()))
    return lines


def _cache_rate(ledger) -> float:
    """输入里有多少来自缓存。"""
    return ledger.cached_tokens / max(1, ledger.prompt_tokens)


def _money(ledger) -> str:
    """按账本的货币显示金额。"""
    from ..llm import symbol

    return f"{symbol(ledger.currency)}{ledger.cost:.4f}"


def _room_lines(health: HealthCenter) -> list[str]:
    """每个房间一行：说了几次、回合多慢、失败几次。"""
    rooms = sorted(
        {
            key.split(".", 1)[0]
            for key in health.counters()
            if "." in key and not key.startswith("llm.")
        }
    )
    lines = []
    for room in rooms:
        says = int(health.counter(f"{room}.says"))
        errors = int(health.counter(f"{room}.turn_errors"))
        parts = [f"说 {says} 次"]
        stats = health.latency(f"{room}.turn_s")
        if stats is not None:
            avg, p95, count = stats
            parts.append(f"回合 平均 {avg:.1f}s / p95 {p95:.1f}s（{count} 次）")
        if errors:
            parts.append(f"失败 {errors} 次")
        lines.append(f"{_room_label(room)}  " + " · ".join(parts))
    return lines


def _room_label(room: str) -> str:
    """房间名换成看得懂的说法：g123 -> 群 123，u456 -> 私聊 456。"""
    if len(room) > 1 and room[0] in ("g", "u") and room[1:].isdigit():
        return ("群 " if room[0] == "g" else "私聊 ") + room[1:]
    return room

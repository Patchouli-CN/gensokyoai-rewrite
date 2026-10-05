"""ws_server 斜杠指令 —— 走 `gensokyoai.command` 子系统（终于启用了）。

老项目同款三条，全部纯本地处理、不调模型、指令消息不进会话：

| 指令 | 别名 | 权限 | 行为 |
|---|---|---|---|
| /help | /帮助 | VISITOR | 按调用者权限列出可用指令 |
| /status | /状态 | USER | 版本/uptime/频道/门控/记忆向量化/费用与 token 统计 |
| /quota | /额度 | USER | 分模块消耗报表 + 账户余额（Moonshot）/ 滚动窗口额度（供应商响应头自报） |

权限来自客户端自报的 role（逐条信封 > 连接 query > 默认 VISITOR），但
**只信回环连接**（server.py 分流层把关）：本机插件可信，公网客户端一律
VISITOR。非回环监听必须配 --token（不配拒启动）。
"""

import time
from dataclasses import dataclass, field

import aiohttp

from ...command import (
    CommandExecutor,
    CommandResult,
    PermissionLevel,
    command,
)
from ...command.decorators import CommandRegistry
from ...core.config import GensokyoConfig
from ...core.session_manager import SessionManager
from ...roleplay.hub import ChannelHub
from ...utils.ratelimit import RATE_LIMITS

WS_COMMANDS = CommandRegistry()
""" ws_server 本地注册表（与将来其他后端的同名指令互不覆盖） """

_ROLE_LEVELS = {
    "owner": PermissionLevel.OWNER,
    "admin": PermissionLevel.ADMIN,
    "member": PermissionLevel.USER,
    "guest": PermissionLevel.VISITOR,
}
""" 客户端自报角色 → 权限等级 """


def role_level(role: str | None) -> PermissionLevel:
    """自报角色字符串转权限等级；未知/缺失按 VISITOR（最低信任级）。"""
    return _ROLE_LEVELS.get((role or "").lower(), PermissionLevel.VISITOR)


@dataclass
class WsCommandState:
    """指令需要访问的运行时状态（由装配层注入 metadata["state"]）。"""

    sessions: SessionManager
    hub: ChannelHub
    config: GensokyoConfig
    version: str = "dev"
    started_at: float = field(default_factory=time.monotonic)


def _format_uptime(seconds: float) -> str:
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes}m{sec}s" if hours else f"{minutes}m{sec}s"


@command(
    name="help",
    aliases=["帮助"],
    description="显示可用指令列表",
    permission=PermissionLevel.VISITOR,
    registry=WS_COMMANDS,
)
async def cmd_help(ctx) -> CommandResult:
    visible = [
        cmd
        for cmd in WS_COMMANDS.list()
        if not cmd.name.startswith("_") and cmd.permission <= ctx.permission
    ]
    lines = ["可用指令："]
    for cmd in visible:
        names = " / ".join(f"/{n}" for n in (cmd.name, *cmd.aliases))
        lines.append(f"{names} - {cmd.description}")
    await ctx.metadata["send"]("\n".join(lines))
    return CommandResult.success("help")


@command(
    name="status",
    aliases=["状态"],
    description="查看系统状态（版本/频道/门控/费用）",
    permission=PermissionLevel.USER,
    registry=WS_COMMANDS,
)
async def cmd_status(ctx) -> CommandResult:
    state: WsCommandState = ctx.metadata["state"]
    cfg = state.config
    channels = state.hub.channel_ids()
    online = sum(state.hub.online_count(c) for c in channels)
    usage = state.sessions.total_usage()
    costs = state.sessions.total_cost()
    cost_text = " / ".join(f"{amount:.4f} {currency}" for currency, amount in costs.items()) or "0"

    lines = [
        f"纯狐引擎 {state.version} | 已运行 {_format_uptime(time.monotonic() - state.started_at)}",
        f"频道: {len(channels)} 个（在线连接 {online}）",
        f"门控: {'on' if cfg.gate.enabled else 'off'}（阈值 {cfg.gate.group_threshold}）"
        f" | 出戏审查: {'on(' + cfg.ooc_judge.mode + ')' if cfg.ooc_judge.enabled else 'off'}"
        f" | 记忆向量化: {'on' if cfg.embedding.enabled else 'off'}",
        f"模型调用: {usage.prompt_tokens}+{usage.completion_tokens} tok（缓存命中 {usage.cached_tokens}）",
        f"累计费用: {cost_text}",
    ]
    await ctx.metadata["send"]("\n".join(lines))
    return CommandResult.success("status")


@command(
    name="quota",
    aliases=["额度"],
    description="查询费用统计与账户额度（分模块消耗 + 余额/窗口额度）",
    permission=PermissionLevel.USER,
    registry=WS_COMMANDS,
)
async def cmd_quota(ctx) -> CommandResult:
    state: WsCommandState = ctx.metadata["state"]
    sessions = state.sessions
    config = state.config

    lines = ["费用信息（自引擎启动以来）："]

    # --- 模块消耗：按 owner 展开（裁判这类纯无状态调用也在账上）---
    breakdown = sessions.usage_breakdown()
    if breakdown:
        lines.append("  模块消耗：")
        ordered = [o for o in _OWNER_ORDER if o in breakdown]
        ordered += sorted(o for o in breakdown if o not in _OWNER_ORDER)
        for owner in ordered:
            usage = breakdown[owner]
            label = _OWNER_LABELS.get(owner, owner)
            model = _owner_model_name(config, owner)
            cost_text = _cost_inline(sessions.cost_by_owner(owner))
            lines.append(
                f"  - {label}({model}): "
                f"输入 {_fmt_tokens(usage.prompt_tokens)} / 输出 {_fmt_tokens(usage.completion_tokens)} tok"
                f"{cost_text}"
            )
    usage = sessions.total_usage()
    total_cost = _cost_inline(sessions.total_cost(), prefix="；费用: ", suffix="")
    lines.append(
        f"  合计: 输入 {_fmt_tokens(usage.prompt_tokens)} / 输出 {_fmt_tokens(usage.completion_tokens)}"
        f" / 缓存命中 {_fmt_tokens(usage.cached_tokens)} tok{total_cost}"
    )

    # --- 账户：预付费余额（Moonshot）+ 滚动窗口额度（响应头自报）---
    lines.append("  账户：")
    account_lines = await _account_lines(config)
    snapshot = RATE_LIMITS.snapshot()
    for base_url, entry in snapshot.items():
        host = base_url.split("//")[-1].split("/")[0]
        for dim, window in entry.windows.items():
            ratio = window.remaining_ratio
            pct = f"{ratio:.0%}" if ratio is not None else f"{window.remaining}"
            reset = (
                time.strftime("（%H:%M 重置）", time.localtime(window.reset_at))
                if window.reset_at
                else ""
            )
            account_lines.append(f"  - {host}: {dim} 剩余 {pct}{reset}")
    if account_lines:
        lines.extend(account_lines)
    else:
        lines.append("  - （本地模型或未上报额度，暂无账户信息）")

    await ctx.metadata["send"]("\n".join(lines))
    return CommandResult.success("quota")


_OWNER_LABELS = {
    "brain.think": "大脑",
    "responder": "表达",
    "gate.think": "裁判",
    "brain.ooc": "出戏审查",
    "memorizer.compress": "记忆蒸馏",
}
""" owner -> 报表里的中文名 """

_OWNER_ORDER = ["brain.think", "responder", "gate.think", "brain.ooc", "memorizer.compress"]
""" 报表展示顺序（未收录的 owner 按字典序排在后面） """


def _owner_model_name(config: GensokyoConfig, owner: str) -> str:
    """owner -> 它吃的模型配置名（对齐 session_factory 的路由约定）。"""
    conf = {
        "brain.think": config.brain,
        "responder": config.responder,
        "brain.ooc": config.ooc or config.brain,
        "memorizer.compress": config.memorizer or config.brain,
    }.get(owner, config.default_model)
    return conf.model_name


def _fmt_tokens(n: int) -> str:
    """token 数缩写：>=1000 用 k 表示（18.2k），以下原样。"""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _cost_inline(costs: dict[str, float], *, prefix: str = " · ", suffix: str = "") -> str:
    """费用 dict -> 行内文本；为空（本地/未计价）给空串。"""
    if not costs:
        return ""
    text = " / ".join(f"{amount:.4f} {currency}" for currency, amount in costs.items())
    return f"{prefix}{text}{suffix}"


async def _account_lines(config: GensokyoConfig) -> list[str]:
    """预付费账户余额行（当前支持 Moonshot /users/me/balance；失败/未配置静默跳过）。"""
    seen: set[tuple[str, str]] = set()
    confs = [config.default_model, config.brain, config.responder]
    if config.ooc is not None:
        confs.append(config.ooc)
    if config.memorizer is not None:
        confs.append(config.memorizer)
    lines: list[str] = []
    for conf in confs:
        if not conf.token or "moonshot" not in conf.base_url:
            continue
        key = (conf.base_url, conf.token)
        if key in seen:
            continue
        seen.add(key)
        balance = await _moonshot_balance(conf)
        if balance:
            lines.append(f"  - Moonshot({conf.model_name}): {balance}")
    return lines


async def _moonshot_balance(conf) -> str | None:
    """查 Moonshot 账户余额；查询失败返回 None（不阻塞指令）。"""
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            resp = await http.get(
                f"{conf.base_url.rstrip('/')}/users/me/balance",
                headers={"Authorization": f"Bearer {conf.token}"},
            )
            if resp.status != 200:
                return None
            data = (await resp.json()).get("data", {})
        return (
            f"余额 ¥{data.get('available_balance', 0):.2f}"
            f"（现金 ¥{data.get('cash_balance', 0):.2f} / 代金券 ¥{data.get('voucher_balance', 0):.2f}）"
        )
    except Exception:
        return None


def build_executor(state: WsCommandState) -> CommandExecutor:
    """装配 ws_server 指令执行器（prefix 模式：只认 / 斜杠，不解析标签）。"""
    executor = CommandExecutor(mode="prefix", registry=WS_COMMANDS)

    # state 注入每个命令的 ctx.metadata（CommandContext 由分流层逐条构造，
    # 这里把共享状态的引用挂到 executor 上，分流层从 executor._state 取）
    executor._state = state  # type: ignore[attr-defined]
    return executor

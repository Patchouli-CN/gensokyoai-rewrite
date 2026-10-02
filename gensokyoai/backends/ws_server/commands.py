"""ws_server 斜杠指令 —— 走 `gensokyoai.command` 子系统（终于启用了）。

老项目同款三条，全部纯本地处理、不调模型、指令消息不进会话：

| 指令 | 别名 | 权限 | 行为 |
|---|---|---|---|
| /help | /帮助 | VISITOR | 按调用者权限列出可用指令 |
| /status | /状态 | USER | 版本/uptime/频道/门控/记忆向量化/费用与 token 统计 |
| /quota | /额度 | USER | 引擎侧计费统计 + Moonshot 账户余额（若配置了 Moonshot 端点） |

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
    description="查询费用统计与 Provider 账户额度",
    permission=PermissionLevel.USER,
    registry=WS_COMMANDS,
)
async def cmd_quota(ctx) -> CommandResult:
    state: WsCommandState = ctx.metadata["state"]
    usage = state.sessions.total_usage()
    costs = state.sessions.total_cost()
    lines = ["本会话引擎计费："]
    if costs:
        for currency, amount in costs.items():
            lines.append(f"  {currency}: {amount:.4f}")
    else:
        lines.append("  （暂无计费记录）")
    lines.append(
        f"  token: 输入 {usage.prompt_tokens} / 输出 {usage.completion_tokens} / 缓存命中 {usage.cached_tokens}"
    )
    balance = await _moonshot_balance(state.config)
    if balance:
        lines.append(balance)
    await ctx.metadata["send"]("\n".join(lines))
    return CommandResult.success("quota")


async def _moonshot_balance(config: GensokyoConfig) -> str | None:
    """配置了 Moonshot 端点时查账户余额；查询失败/未配置返回 None（不阻塞指令）。"""
    conf = next(
        (
            c
            for c in (config.default_model, config.brain, config.responder)
            if c.token and "moonshot" in c.base_url
        ),
        None,
    )
    if conf is None:
        return None
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
            f"Moonshot 账户余额: ¥{data.get('available_balance', 0):.2f}"
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

"""命令子模块与健康中心测试。"""

from gensokyoai.command import Command, CommandContext, CommandExecutor, permission_of
from gensokyoai.command.builtin import build_executor
from gensokyoai.health import HealthCenter

# ---------- 命令执行器 ----------


async def test_execute_dispatches_with_args():
    executor = CommandExecutor()

    async def echo(ctx, args: str) -> str:
        return f"{ctx.issuer}说{args}"

    executor.register(Command(name="echo", handler=echo))
    reply = await executor.execute("/echo 你好", CommandContext(issuer="帕秋莉"))
    assert reply == "帕秋莉说你好"


async def test_alias_dispatches_to_same_command():
    executor = CommandExecutor()

    async def ping(ctx, args: str) -> str:
        return "pong"

    executor.register(Command(name="ping", handler=ping, aliases=("碰",)))
    assert await executor.execute("/碰", CommandContext()) == "pong"


async def test_unknown_and_non_command_silent():
    """未知命令与非命令一律 None（静默，不暴露命令面）"""
    executor = CommandExecutor()
    assert await executor.execute("/不存在", CommandContext()) is None
    assert await executor.execute("普通消息", CommandContext()) is None


async def test_permission_insufficient_silent():
    """权限不足静默（不提示、不报错）"""
    executor = CommandExecutor()

    async def admin_only(ctx, args: str) -> str:
        return "ok"

    executor.register(Command(name="secret", handler=admin_only, permission="admin"))
    assert (
        await executor.execute("/secret", CommandContext(permission="visitor")) is None
    )
    assert await executor.execute("/secret", CommandContext(permission="admin")) == "ok"


def test_duplicate_registration_rejected():
    executor = CommandExecutor()

    async def noop(ctx, args: str) -> str:
        return ""

    executor.register(Command(name="x", handler=noop, aliases=("y",)))
    for dup in (
        Command(name="x", handler=noop),
        Command(name="z", handler=noop, aliases=("y",)),
    ):
        try:
            executor.register(dup)
            raise AssertionError("应拒绝重复注册")
        except ValueError:
            pass


def test_permission_of_maps_roles():
    """接入侧角色映射：owner/admin 之外一律 visitor"""
    assert permission_of("owner") == "owner"
    assert permission_of("admin") == "admin"
    assert permission_of("member") == "visitor"
    assert permission_of("") == "visitor"


def test_visible_respects_permission():
    """/help 只列得出自己权限够得着的命令"""
    executor = build_executor(HealthCenter())
    assert {c.name for c in executor.visible("visitor")} == {"help", "doctor"}


# ---------- 内置命令 ----------


async def test_builtin_doctor_reports_health():
    reply = await build_executor(HealthCenter()).execute("/体检", CommandContext())
    assert reply is not None and "运行" in reply


async def test_builtin_help_lists_commands():
    reply = await build_executor(HealthCenter()).execute("/help", CommandContext())
    assert "/doctor" in reply and "/help" in reply


# ---------- 健康中心 ----------


def test_health_counter_gauge_sample_report():
    clock = _Clock()
    health = HealthCenter(clock=clock)
    health.count("g1.turns", 3)
    health.gauge("rooms", lambda: 2)
    for value in (1.0, 2.0, 3.0):
        health.observe("g1.turn_s", value)
    clock.tick(60)
    report = health.report()
    assert "g1.turns=3" in report
    assert "rooms=2" in report
    assert "avg=2.00" in report


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds

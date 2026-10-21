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


# ---------- 合并重复（同房间、同命令、同参数） ----------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _counting_executor(clock: _Clock, *, coalesce_s: float = 3.0):
    calls: list[str] = []

    async def handler(ctx, args: str) -> str:
        calls.append(f"{ctx.room}:{args}")
        return f"结果{len(calls)}"

    executor = CommandExecutor(clock=clock)
    executor.register(Command(name="balance", handler=handler, coalesce_s=coalesce_s))
    return executor, calls


async def test_same_room_repeats_are_coalesced():
    """A、B、C 在同一个群连查余额：只查一次"""
    clock = _Clock()
    executor, calls = _counting_executor(clock)

    first = await executor.execute("/balance", CommandContext(room="g1"))
    second = await executor.execute("/balance", CommandContext(room="g1"))
    clock.tick(0.5)
    third = await executor.execute("/balance", CommandContext(room="g1"))

    assert first == "结果1"
    assert second is None and third is None, "回复大家看得见，后来的静默"
    assert calls == ["g1:"], "只真查了一次"


async def test_different_rooms_are_not_coalesced():
    """不同群互相看不见，各查各的"""
    clock = _Clock()
    executor, calls = _counting_executor(clock)

    await executor.execute("/balance", CommandContext(room="g1"))
    await executor.execute("/balance", CommandContext(room="g2"))

    assert calls == ["g1:", "g2:"]


async def test_window_expires():
    """过了窗口还是可以再查"""
    clock = _Clock()
    executor, calls = _counting_executor(clock, coalesce_s=3.0)

    await executor.execute("/balance", CommandContext(room="g1"))
    clock.tick(3.5)
    again = await executor.execute("/balance", CommandContext(room="g1"))

    assert again is not None
    assert len(calls) == 2


async def test_different_args_are_not_coalesced():
    """参数不同算不同请求"""
    clock = _Clock()
    executor, calls = _counting_executor(clock)

    await executor.execute("/balance 今天", CommandContext(room="g1"))
    await executor.execute("/balance 昨天", CommandContext(room="g1"))

    assert calls == ["g1:今天", "g1:昨天"]


async def test_commands_without_window_never_coalesce():
    """没声明窗口的命令（/help 这类）照常每次都执行"""
    clock = _Clock()
    executor = CommandExecutor(clock=clock)
    calls = []

    async def handler(ctx, args: str) -> str:
        calls.append(1)
        return "ok"

    executor.register(Command(name="help", handler=handler))

    await executor.execute("/help", CommandContext(room="g1"))
    await executor.execute("/help", CommandContext(room="g1"))

    assert len(calls) == 2


async def test_in_flight_query_also_coalesces():
    """A 的查询还没回来，B 又来一次：B 也该被吞掉"""
    clock = _Clock()
    started: list[str] = []
    release = None

    async def handler(ctx, args: str) -> str:
        started.append(ctx.issuer)
        if release is not None:
            await release.wait()
        return "余额"

    executor = CommandExecutor(clock=clock)
    executor.register(Command(name="balance", handler=handler, coalesce_s=3.0))

    import asyncio

    release = asyncio.Event()
    task = asyncio.create_task(
        executor.execute("/balance", CommandContext(issuer="A", room="g1"))
    )
    await asyncio.sleep(0)
    second = await executor.execute("/balance", CommandContext(issuer="B", room="g1"))
    release.set()
    await task

    assert second is None
    assert started == ["A"], "B 没触发第二次查询"

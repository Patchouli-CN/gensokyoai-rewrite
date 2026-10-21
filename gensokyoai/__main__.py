"""入口 —— 控制台聊天 / WebSocket 服务（一群一房）。

用法：
  控制台: python -m gensokyoai --character <卡面.yaml> [--scene chat] [--plugin-dir DIR]
  服务:   python -m gensokyoai --character <卡面.yaml> --serve [--port 8082] [--token T]
  场景:   python -m gensokyoai --list-scenes
"""

import argparse
import asyncio
from pathlib import Path

import msgspec

from . import scenes as _builtin_scenes  # noqa: F401  注册内置场景
from .api import scenes as registered_scenes
from .bgtasks import BgTasks
from .character import load_card
from .command.builtin import build_executor
from .dmn import DmnKeeper
from .health import HealthCenter
from .hub import Hub
from .loop import Loop, SceneContext
from .memory import Memory
from .mind import Mind
from .mouth import ConsoleMouth, WsMouth
from .network.aiohttp_ws import AiohttpWebsocketServer
from .plugin import discover, load
from .room import Room, RoomLog
from .settings import load_settings
from .utils.logger import LoggerManager, setup_logging
from .ws_server import Gateway

_scene_log = LoggerManager.get_logger("SCENE")


async def _tidy(loop: Loop) -> None:
    """静息整理：记忆超过压力线时压缩。"""
    memory = loop.memory
    if memory is not None and memory.needs_consolidation():
        await memory.consolidate(loop.mind.model)


def _loop_factory(
    mind: Mind,
    mouth_for,
    storage: Path | None,
    health=None,
    tasks: BgTasks | None = None,
    keeper: DmnKeeper | None = None,
    scene_cls=None,
    window: int = 30,
):
    def factory(room_id: str) -> Loop:
        memory = (
            Memory(storage / room_id / "memory.jsonl")
            if storage is not None
            else Memory(None)
        )
        if health is not None:
            health.gauge(f"{room_id}.memory", lambda m=memory: m.size)
        log = (
            RoomLog(storage / room_id / "events.jsonl", tasks)
            if storage is not None and tasks is not None
            else None
        )
        mouth = mouth_for(room_id)
        scene = (
            scene_cls(
                SceneContext(mouth=mouth, name=mind.name, room=room_id, log=_scene_log)
            )
            if scene_cls is not None
            else None
        )
        loop = Loop(
            room=Room(room_id, log=log),
            mind=mind,
            mouth=mouth,
            memory=memory,
            health=health,
            scene=scene,
            window=window,
        )
        if keeper is not None:
            keeper.watch(room_id, loop.room.last_activity, lambda: _tidy(loop))
        return loop

    return factory


async def _console(args, card, mind: Mind, scene_cls) -> None:
    tasks = BgTasks()
    keeper = DmnKeeper()
    tasks.every("dmn", 60.0, keeper.patrol)
    loop = _loop_factory(
        mind,
        lambda _id: ConsoleMouth(),
        Path(args.data),
        tasks=tasks,
        keeper=keeper,
        scene_cls=scene_cls,
        window=args.window,
    )("console")
    task = asyncio.create_task(loop.run())
    try:
        while True:
            try:
                text = (await asyncio.to_thread(input, f"{args.user}: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if text:
                loop.notify("message", args.user, text, user_id=args.user)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await tasks.shutdown()


async def _serve(args, card, mind: Mind, scene_cls) -> None:
    health = HealthCenter()
    tasks = BgTasks(health)
    keeper = DmnKeeper()
    tasks.every("dmn", 60.0, keeper.patrol)
    hub = Hub(
        _loop_factory(
            mind,
            lambda _id: WsMouth(),
            Path(args.data),
            health,
            tasks=tasks,
            keeper=keeper,
            scene_cls=scene_cls,
            window=args.window,
        )
    )
    health.gauge("rooms", lambda: len(hub.room_ids()))
    gateway = Gateway(
        hub,
        token=args.token,
        executor=build_executor(health, mind.model),
        max_rooms=args.max_rooms,
    )
    server = AiohttpWebsocketServer(args.host, args.port, validate=gateway.validate)
    serving = server.run()
    print(f"WS 服务: ws://{args.host}:{args.port}/ws/{{channel}}")
    consumer = asyncio.create_task(gateway.run(server))
    try:
        await asyncio.wait({consumer, serving}, return_when=asyncio.FIRST_COMPLETED)
        if serving.done() and not serving.cancelled() and serving.exception():
            raise serving.exception()  # 监听起不来（端口占用等）就别假装在服务
    finally:
        consumer.cancel()
        serving.cancel()
        await asyncio.gather(consumer, serving, return_exceptions=True)
        await hub.shutdown()
        await tasks.shutdown()
        print("已停止")


async def main() -> None:
    """入口：默认控制台；--serve 起 ws 服务。"""
    parser = argparse.ArgumentParser(prog="gensokyoai", description="GensokyoAI v2")
    parser.add_argument("--character", help="角色卡 YAML 路径")
    parser.add_argument(
        "--settings",
        default="",
        help="模型与计费配置（默认 config/settings.yaml，模板见 example）",
    )
    parser.add_argument("--model", default="", help="临时覆盖配置里的端点地址")
    parser.add_argument(
        "--model-timeout",
        type=float,
        default=0.0,
        help="临时覆盖配置里的补全时限（秒）",
    )
    parser.add_argument("--user", default="你", help="控制台里你的称呼")
    parser.add_argument("--data", default="data/rooms", help="记忆与房间日志落盘根目录")
    parser.add_argument("--serve", action="store_true", help="起 WebSocket 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--token", default=None, help="接入令牌")
    parser.add_argument(
        "--max-rooms",
        type=int,
        default=64,
        help="同时存在的房间上限（新频道超过即拒连 503）",
    )
    parser.add_argument(
        "--window", type=int, default=30, help="一次投递给模型多少条未读"
    )
    parser.add_argument(
        "--scene", default="chat", help="用哪个场景（见 --list-scenes）"
    )
    parser.add_argument(
        "--plugin-dir",
        action="append",
        default=[],
        metavar="DIR",
        help="插件目录（可重复；每个 .py 当一个插件）",
    )
    parser.add_argument("--list-scenes", action="store_true", help="列出可用场景后退出")
    args = parser.parse_args()

    setup_logging("INFO", True, None)
    available = load(discover([Path(d) for d in args.plugin_dir]))

    if args.list_scenes:
        for name in sorted(available):
            print(f"{name:16s} {available[name].__module__}")
        return
    if not registered_scenes():
        raise SystemExit("没有可用场景")
    scene_cls = available.get(args.scene)
    if scene_cls is None:
        raise SystemExit(
            f"未知场景: {args.scene!r}（可用: {', '.join(sorted(available))}）"
        )
    if not args.character:
        raise SystemExit("缺少 --character")

    card = load_card(args.character)
    settings = load_settings(args.settings or None)
    if args.model_timeout:
        settings = msgspec.structs.replace(
            settings,
            model=msgspec.structs.replace(settings.model, timeout=args.model_timeout),
        )
    model = settings.build_model(base_url=args.model)
    capabilities = await settings.adopt_capabilities(model)
    LoggerManager.get_logger("SETTINGS").info(
        f"模型能力: {', '.join(sorted(capabilities))}（{model.name or '未命名'}）"
    )
    mind = Mind(
        model,
        card.name,
        card.persona,
    )
    try:
        if args.serve:
            await _serve(args, card, mind, scene_cls)
        else:
            await _console(args, card, mind, scene_cls)
    finally:
        await mind.close()


if __name__ == "__main__":
    asyncio.run(main())

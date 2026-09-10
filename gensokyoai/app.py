"""程序装配入口 —— 仓库内 `main.py` 与安装后的 `gensokyoai` 命令共用同一条装配路径。

此前装配逻辑写在**仓库根目录**的 `main.py` 里，而 wheel 只打包 `gensokyoai/`：
`pip install` 之后既没有 console 入口，也没有配置文件 —— 等于装完跑不起来。

现在把装配收进包里：

- `build_world()`：纯装配（配置 → 会话 → 角色 → 口层 → TouhouWorld），
  CLI / WebSocket 后端 / 测试复用同一条路径
- `main()`：控制台 CLI 入口，`[project.scripts]` 指向它
- `resolve_resource()`：资源解析优先工作目录（仓库开发），回落到包内自带默认值（安装后）
"""

import argparse
import asyncio
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from .core.bootstrap import discover_all
from .core.config import load_config
from .core.resource import ResourceGate
from .core.session_factory import build_resource_gate, build_session_manager
from .core.session_manager import SessionManager
from .eyes.base import Perceiver
from .eyes.perceiver import ConsolePerceiver
from .mouth.base import Mouth
from .mouth.console import ConsoleMouth
from .roleplay.character import Character, load_character
from .roleplay.loop import TouhouWorld
from .utils.logger import LoggerManager, setup_logging

DEFAULT_CONFIG = "config/settings.yaml"
""" 默认配置文件（相对路径）"""

DEFAULT_CHARACTER = "config/roles/SaigyoujiYuyuko.yaml"
""" 默认角色卡（相对路径）"""

_RESOURCE_ROOT = "_resources"
""" 包内自带资源的目录名 """


def resolve_resource(relative: str | Path) -> Path:
    """解析资源路径：优先工作目录，回落到包内自带资源。

    同时服务两种用法：

    - **仓库内开发**：直接用仓库根目录的 `config/`（可随意修改）
    - **安装后使用**：用 wheel 里自带的默认 `config/`；也可以用 `--config` 指定自己的

    Args:
        relative: 相对路径，如 "config/settings.yaml"

    Returns:
        Path: 解析到的路径

    Raises:
        FileNotFoundError: 工作目录与包内均不存在
    """
    local = Path(relative)
    if local.exists():
        return local.resolve()

    bundled = files("gensokyoai").joinpath(_RESOURCE_ROOT, *Path(relative).parts)
    bundled_path = Path(str(bundled))
    if bundled_path.exists():
        return bundled_path.resolve()

    raise FileNotFoundError(
        f"找不到资源: {relative}（工作目录与包内自带资源均无）；"
        f"可用 --config / --character 显式指定"
    )


def build_session_and_character(
    *,
    config_path: str | Path | None = None,
    character_path: str | Path | None = None,
    gate: ResourceGate | None = None,
) -> tuple[SessionManager, Character]:
    """装配「会话管理器 + 角色」（**不建世界**）。

    多世界 / 多频道后端（如 WebSocket 的 ChannelHub）自己造世界，只需要这两样。

    Args:
        config_path: 配置文件路径；None 用默认（先工作目录，后包内自带）
        character_path: 角色卡路径；None 用默认
        gate: 资源闸门；None 时按配置自建

    Returns:
        tuple[SessionManager, Character]: 装配好的会话管理器与角色
    """
    discover_all()

    config = load_config(config_path or resolve_resource(DEFAULT_CONFIG))
    sessions = build_session_manager(
        config, gate if gate is not None else build_resource_gate(config)
    )
    character: Character = load_character(character_path or resolve_resource(DEFAULT_CHARACTER))
    return sessions, character


def build_world(
    *,
    config_path: str | Path | None = None,
    character_path: str | Path | None = None,
    session_id: str = "console",
    gate: ResourceGate | None = None,
    eye: Perceiver | None = None,
    mouth: Mouth | None = None,
    storage_dir: str | Path | None = None,
) -> TouhouWorld:
    """按配置装配一个可直接 `start()` 的世界（CLI / 测试共用）。

    Args:
        config_path: 配置文件路径；None 用默认（先工作目录，后包内自带）
        character_path: 角色卡路径；None 用默认
        session_id: 会话标识（记忆与快照按它隔离）
        gate: 资源闸门；None 时按配置自建
        eye: 感知器；None 时用控制台感知器
        mouth: 口层；None 时用控制台口层
        storage_dir: 持久化根目录；None 用 TouhouWorld 默认（当前工作目录下的 `data`）

    Returns:
        TouhouWorld: 装配完成、可直接启动的世界
    """
    sessions, character = build_session_and_character(
        config_path=config_path, character_path=character_path, gate=gate
    )
    world_kwargs: dict[str, Any] = {}
    if storage_dir is not None:
        world_kwargs["storage_dir"] = storage_dir
    return TouhouWorld(
        eye=eye if eye is not None else ConsolePerceiver(sender="你"),
        character=character,
        sessions=sessions,
        mouth=mouth if mouth is not None else ConsoleMouth(),
        session_id=session_id,
        **world_kwargs,
    )


def build_parser() -> argparse.ArgumentParser:
    """构建控制台模式的命令行解析器。

    Returns:
        argparse.ArgumentParser: 解析器
    """
    parser = argparse.ArgumentParser(
        prog="gensokyoai",
        description="幻想乡 AI 角色扮演引擎（控制台模式）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"配置文件路径（默认 {DEFAULT_CONFIG}；工作目录找不到则用包内自带）",
    )
    parser.add_argument(
        "--character",
        default=None,
        help=f"角色卡路径（默认 {DEFAULT_CHARACTER}）",
    )
    parser.add_argument("--session", default="console", help="会话标识（记忆/快照按它隔离）")
    parser.add_argument("--log-level", default="INFO", help="日志级别（TRACE/DEBUG/INFO/...）")
    parser.add_argument("--log-file", default=None, help="日志文件路径；不传则只输出控制台")
    return parser


def main(argv: list[str] | None = None) -> int:
    """控制台入口（`[project.scripts]` 指向这里）。

    Args:
        argv: 命令行参数；None 时取 `sys.argv[1:]`

    Returns:
        int: 进程退出码（0 正常，2 资源缺失）
    """
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, True, args.log_file)
    logger = LoggerManager.get_logger("MAIN")

    try:
        world = build_world(
            config_path=args.config,
            character_path=args.character,
            session_id=args.session,
        )
    except FileNotFoundError as err:
        print(f"启动失败: {err}", file=sys.stderr)
        return 2

    logger.info("启动幻想乡...")
    try:
        asyncio.run(world.start())
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    return 0


__all__ = [
    "DEFAULT_CHARACTER",
    "DEFAULT_CONFIG",
    "build_parser",
    "build_session_and_character",
    "build_world",
    "main",
    "resolve_resource",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

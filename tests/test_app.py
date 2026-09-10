"""装配入口测试：资源解析 / 世界装配 / CLI / 安装后可运行性"""

from pathlib import Path

import pytest

from gensokyoai.app import (
    DEFAULT_CHARACTER,
    DEFAULT_CONFIG,
    build_parser,
    build_session_and_character,
    build_world,
    main,
    resolve_resource,
)

_ROOT = Path(__file__).resolve().parent.parent
""" 仓库根目录（测试从根目录跑）"""


def test_resolve_resource_prefers_working_dir(tmp_path, monkeypatch):
    """工作目录存在同名文件时优先用它（仓库内开发场景），且返回绝对路径"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "config" / "settings.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("default_model:\n  model_name: local\n", encoding="utf-8")

    resolved = resolve_resource(DEFAULT_CONFIG)
    assert resolved == target.resolve()
    assert resolved.is_absolute()


def test_resolve_resource_missing_raises_actionable_error():
    """两处都找不到时报错，并提示可用 --config 指定（而不是抛裸栈）"""
    with pytest.raises(FileNotFoundError) as exc:
        resolve_resource("config/绝对不存在的文件.yaml")
    assert "--config" in str(exc.value)


def test_resolve_resource_falls_back_to_bundled(tmp_path, monkeypatch):
    """工作目录没有时回落到包内自带资源 —— 这就是「安装后」的取资源路径"""
    import gensokyoai.app as app_module

    fake_package = tmp_path / "fake_package"
    bundled = fake_package / "_resources" / "config" / "settings.yaml"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("default_model:\n  model_name: bundled\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)  # 工作目录里没有 config/
    monkeypatch.setattr(app_module, "files", lambda _package: fake_package)

    resolved = app_module.resolve_resource("config/settings.yaml")
    assert resolved == bundled.resolve()
    assert "local" not in resolved.read_text(encoding="utf-8")


def test_build_session_and_character_returns_assembled_pair():
    """装配出「会话管理器 + 角色」；各 owner 的路由已就绪"""
    sessions, character = build_session_and_character(
        config_path=_ROOT / DEFAULT_CONFIG,
        character_path=_ROOT / DEFAULT_CHARACTER,
    )
    assert character.name
    assert character.prompt
    for owner in ("brain.think", "responder", "brain.ooc", "memorizer.compress"):
        assert owner in sessions._backends


def test_build_world_uses_injected_io_and_session_id():
    """注入的 eye/mouth 被采用，session_id 透传"""
    from gensokyoai.eyes.queue import QueuePerceiver
    from gensokyoai.mouth.broadcast import BroadcastMouth

    eye, mouth = QueuePerceiver(), BroadcastMouth()
    world = build_world(
        config_path=_ROOT / DEFAULT_CONFIG,
        character_path=_ROOT / DEFAULT_CHARACTER,
        eye=eye,
        mouth=mouth,
        session_id="t-app",
    )

    assert world.eye is eye
    assert world.mouth is mouth
    assert world.session_id == "t-app"


def test_build_world_defaults_to_console_io():
    """不注入时默认给控制台感知器与口层"""
    from gensokyoai.eyes.perceiver import ConsolePerceiver
    from gensokyoai.mouth.console import ConsoleMouth

    world = build_world(
        config_path=_ROOT / DEFAULT_CONFIG,
        character_path=_ROOT / DEFAULT_CHARACTER,
    )
    assert isinstance(world.eye, ConsolePerceiver)
    assert isinstance(world.mouth, ConsoleMouth)


def test_parser_defaults_and_overrides():
    """CLI 默认值与覆盖"""
    defaults = build_parser().parse_args([])
    assert defaults.config is None
    assert defaults.character is None
    assert defaults.session == "console"
    assert defaults.log_level == "INFO"

    overridden = build_parser().parse_args(
        ["--config", "a.yaml", "--session", "s1", "--log-level", "DEBUG"]
    )
    assert overridden.config == "a.yaml"
    assert overridden.session == "s1"
    assert overridden.log_level == "DEBUG"


def test_main_returns_exit_code_on_missing_resource(tmp_path, monkeypatch, capsys):
    """资源缺失时返回退出码 2 并给出可读提示，不抛栈"""
    monkeypatch.chdir(tmp_path)
    code = main(["--config", "不存在.yaml", "--log-level", "CRITICAL"])

    assert code == 2
    assert "启动失败" in capsys.readouterr().err


def test_ws_entry_has_parser_and_reuses_assembly():
    """WS 入口有自己的参数，且装配复用 app.build_session_and_character"""
    from gensokyoai.backends.ws_server.server import build_parser as ws_parser
    from gensokyoai.backends.ws_server.server import main as ws_main

    args = ws_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8081
    assert callable(ws_main)

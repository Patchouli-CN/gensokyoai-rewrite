"""眼层平台配置加载：单文件类型化 / 缺失回落 / 类型校验 / 多文件目录解析"""

import msgspec
import pytest

from gensokyoai.core.config import load_eye_config, resolve_eye_config_dir
from gensokyoai.satori.perceiver import ConsoleEyeSettings, ConsolePerceiver


def test_load_eye_config_reads_typed(tmp_path):
    """单文件平台：YAML -> 强类型配置"""
    (tmp_path / "console.yaml").write_text('sender: "灵梦"\n', encoding="utf-8")
    settings = load_eye_config("console", ConsoleEyeSettings, base_dir=tmp_path)
    assert settings is not None
    assert settings.sender == "灵梦"


def test_load_eye_config_missing_returns_none(tmp_path):
    """配置文件不存在：None（调用方用 schema 默认值兜底）"""
    assert load_eye_config("不存在", ConsoleEyeSettings, base_dir=tmp_path) is None


def test_load_eye_config_validates_types(tmp_path):
    """字段类型不符：ValidationError（msgspec 不做 int -> str 强转）"""
    (tmp_path / "bad.yaml").write_text("sender: 42\n", encoding="utf-8")
    with pytest.raises(msgspec.ValidationError):
        load_eye_config("bad", ConsoleEyeSettings, base_dir=tmp_path)


def test_resolve_eye_config_dir(tmp_path):
    """多文件平台：目录存在则返回，否则 None"""
    (tmp_path / "nb2").mkdir()
    assert resolve_eye_config_dir("nb2", base_dir=tmp_path) == tmp_path / "nb2"
    assert resolve_eye_config_dir("不存在", base_dir=tmp_path) is None


def test_console_perceiver_uses_settings():
    """console eye：显式配置 > 默认 schema；None 与缺省等价"""
    assert ConsolePerceiver(ConsoleEyeSettings(sender="灵梦"))._sender == "灵梦"
    assert ConsolePerceiver()._sender == "你"
    assert ConsolePerceiver(None)._sender == "你"

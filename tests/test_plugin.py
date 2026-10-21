"""插件加载 —— 目录发现、坏插件跳过、版本不匹配。"""

from gensokyoai.api import scenes
from gensokyoai.plugin import discover, load

_GOOD = """
from gensokyoai.api import scene

@scene(name="{name}", prompt="{name} 场景")
class Demo:
    pass
"""


def _write(tmp_path, name: str, text: str):
    path = tmp_path / f"{name}.py"
    path.write_text(text, encoding="utf-8")
    return path


def test_discover_and_mount_from_directory(tmp_path):
    """目录里的 .py 被扫到并挂载"""
    _write(tmp_path, "demo", _GOOD.format(name="t_demo"))

    modules = discover([tmp_path])
    available = load(modules)

    assert "t_demo" in available
    assert available["t_demo"].__module__.startswith("gensokyoai_plugin_demo")


def test_broken_plugin_does_not_stop_others(tmp_path):
    """坏插件只跳过，好插件照常挂载"""
    _write(tmp_path, "bad", "raise RuntimeError('炸')\n")
    _write(tmp_path, "ok", _GOOD.format(name="t_ok"))

    available = load(discover([tmp_path]))

    assert "t_ok" in available


def test_version_mismatch_drops_that_plugin(tmp_path):
    """API 版本对不上：这个插件的场景不挂"""
    _write(tmp_path, "old", 'API_VERSION = "0"\n' + _GOOD.format(name="t_old"))
    _write(tmp_path, "new", _GOOD.format(name="t_new"))

    available = load(discover([tmp_path]))

    assert "t_old" not in available
    assert "t_new" in available


def test_private_modules_are_skipped(tmp_path):
    """下划线开头的文件不当插件"""
    _write(tmp_path, "_hidden", _GOOD.format(name="t_hidden"))

    assert discover([tmp_path]) == []
    assert "t_hidden" not in scenes()


def test_missing_directory_is_harmless(tmp_path):
    """目录不存在只当没插件"""
    assert discover([tmp_path / "nope"]) == []

"""插件加载 —— 库主动发现并挂载场景插件，插件不用被谁 import。

两条发现通道：
- entry points（`gensokyoai.plugins` 组）：pip 安装的正式插件，装完自动出现
- 目录扫描：随手丢进去的 .py 模块，开发期够用

坏插件只记日志并跳过，不影响启动。
"""

import importlib.util
import sys
from importlib.metadata import entry_points
from pathlib import Path

from .api import API_VERSION, drop_module, scenes
from .utils.logger import LoggerManager

_logger = LoggerManager.get_logger("PLUGIN")

ENTRY_GROUP = "gensokyoai.plugins"
""" pip 插件的 entry point 组名 """


def discover(dirs: list[Path] | tuple[Path, ...] = ()) -> list:
    """按 entry points 与目录找插件模块。

    Args:
        dirs: 额外扫描的目录（每个 `.py` 当一个插件）

    Returns:
        list: 导入成功的模块
    """
    modules = []
    for entry in entry_points(group=ENTRY_GROUP):
        try:
            modules.append(entry.load())
        except Exception:
            _logger.exception(f"插件 {entry.name} 导入失败，跳过")
    for folder in dirs:
        for path in sorted(Path(folder).glob("*.py")):
            if path.name.startswith("_"):
                continue
            module = _load_file(path)
            if module is not None:
                modules.append(module)
    return modules


def load(modules: list) -> dict[str, type]:
    """检查版本并返回可挂载的场景。

    Args:
        modules: discover() 的结果

    Returns:
        dict[str, type]: 场景名 -> 场景类
    """
    from . import scenes as _builtin  # noqa: F401  确保内置场景已注册

    for module in modules:
        declared = getattr(module, "API_VERSION", None)
        if declared is not None and str(declared) != API_VERSION:
            drop_module(module.__name__)
            _logger.warning(
                f"插件 {module.__name__} 要求 API {declared}，"
                f"本库是 {API_VERSION}，已跳过"
            )
    available = scenes()
    _logger.info(f"已挂载场景: {', '.join(sorted(available)) or '无'}")
    return available


def _load_file(path: Path):
    """把一个 .py 当模块导入；失败返回 None。"""
    name = f"gensokyoai_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            _logger.warning(f"插件 {path} 无法导入，跳过")
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        _logger.exception(f"插件 {path} 导入失败，跳过")
        sys.modules.pop(name, None)
        return None

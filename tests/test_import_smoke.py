"""全量导入冒烟 —— 架构可编译性验证"""

import importlib
import pkgutil

import gensokyoai


def test_all_modules_import():
    """所有子模块必须可导入（stub 阶段即验证分层与契约无语法/依赖错误）"""
    failed: list[str] = []
    for m in pkgutil.walk_packages(gensokyoai.__path__, "gensokyoai."):
        try:
            importlib.import_module(m.name)
        except Exception as e:
            failed.append(f"{m.name}: {e!r}")
    assert not failed, "导入失败的模块:\n" + "\n".join(failed)


def test_registry_discovers_extensions():
    """导入后声明式扩展应已注册进全局表"""
    from gensokyoai.core.registry import Registry

    assert Registry.get("qwen_local").__name__ == "QwenLocalProvider"

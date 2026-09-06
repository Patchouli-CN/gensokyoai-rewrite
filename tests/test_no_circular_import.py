"""循环导入检查 —— L3 业务模块横向 import 检查（架构文档 §7.3）"""

import ast
from pathlib import Path

GENSOKYOAI_DIR = Path(__file__).resolve().parent.parent / "gensokyoai"

FORBIDDEN_CROSS_IMPORTS = {
    "eyes": {"brain", "responder", "memorizer", "health", "roleplay"},
    "brain": {"eyes", "responder", "memorizer", "health", "roleplay"},
    "responder": {"eyes", "brain", "memorizer", "health", "roleplay"},
    "memorizer": {"eyes", "brain", "responder", "health", "roleplay"},
    "health": {"eyes", "brain", "responder", "memorizer", "roleplay"},
    "roleplay": {"eyes", "brain", "responder", "memorizer", "health"},
}


def _import_targets(tree: ast.AST) -> set[str]:
    """收集 import 涉及的 gensokyoai 子包名（相对导入解析为包名）"""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 1:
                # 同包内相对导入: from .registry import x —— 不跨包
                continue
            if node.level > 1:
                # 跨包相对导入: ..core.x -> core
                targets.add(module.split(".")[0])
            else:
                parts = module.split(".")
                if parts[0] == "gensokyoai" and len(parts) > 1:
                    targets.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "gensokyoai" and len(parts) > 1:
                    targets.add(parts[1])
    return targets


def _py_files(module: str):
    return [f for f in (GENSOKYOAI_DIR / module).glob("*.py") if f.name != "__init__.py"]


def test_no_cross_business_imports():
    """业务模块之间禁止直接 import，通信走事件总线"""
    for module, forbidden in FORBIDDEN_CROSS_IMPORTS.items():
        for py_file in _py_files(module):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            bad = _import_targets(tree) & forbidden
            assert not bad, f"❌ {py_file} 非法导入业务模块: {bad}"


def test_utils_is_leaf():
    """utils 是纯叶子，不依赖项目内任何模块"""
    for py_file in _py_files("utils"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        bad = _import_targets(tree) - {"utils"}
        assert not bad, f"❌ {py_file} 依赖了项目内模块: {bad}"


def test_core_low_layers_only():
    """core 只能依赖 utils/schemas/core，不许依赖 models 和业务模块"""
    for py_file in _py_files("core"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        bad = _import_targets(tree) - {"utils", "schemas", "core"}
        assert not bad, f"❌ {py_file} 非法依赖: {bad}"

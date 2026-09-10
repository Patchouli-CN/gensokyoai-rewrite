"""分层依赖检查 —— 业务子包之间零直接 import，通信走事件总线 / L4 注入（架构文档 §7.3）

结构说明：brain/responder/memorizer/health 收进 core/ 作为引擎内核，
eyes（平台感知）、roleplay（领域内容）、models（模型接入）是可替换边缘层。
内核子包之间依然禁止互相 import，否则 core 会退化成一锅巨石。
"""

import ast
from pathlib import Path

GENSOKYOAI_DIR = Path(__file__).resolve().parent.parent / "gensokyoai"

FORBIDDEN_CROSS_IMPORTS = {
    "core/brain": {"core/responder", "core/memorizer", "core/health", "eyes", "roleplay"},
    "core/responder": {"core/brain", "core/memorizer", "core/health", "eyes", "roleplay"},
    "core/memorizer": {"core/brain", "core/responder", "core/health", "eyes", "roleplay"},
    "core/health": {"core/brain", "core/responder", "core/memorizer", "eyes", "roleplay"},
    "eyes": {"core/brain", "core/responder", "core/memorizer", "core/health", "roleplay"},
    # roleplay 不设禁入清单：它是角色扮演场景的装配层（类似 L4），
    # 允许组装 core 业务与 eyes；但反向依赖（core/eyes -> roleplay）仍然禁止。
}

CORE_TOP_FORBIDDEN = {
    "core/brain",
    "core/responder",
    "core/memorizer",
    "core/health",
    "eyes",
    "roleplay",
    "models",
}
""" core 顶层基建（session_manager/event_bus/...）只准依赖 schemas/prompts/utils """


def _package_of(py_file: Path) -> tuple[str, ...]:
    """文件相对 gensokyoai/ 的包路径，如 core/brain/engine.py -> ("core", "brain")"""
    return py_file.relative_to(GENSOKYOAI_DIR).parts[:-1]


def _own_key(py_file: Path) -> str:
    """文件所属模块键：core 子包为 core/brain 两段式，其余取首段"""
    rel = py_file.relative_to(GENSOKYOAI_DIR).parts
    if len(rel) >= 3 and rel[0] == "core":
        return f"core/{rel[1]}"
    return rel[0]


def _module_key(rel: tuple[str, ...]) -> str:
    """gensokyoai 内相对模块路径 -> 模块键（两段式优先）"""
    if rel and rel[0] == "core" and len(rel) >= 2:
        return f"core/{rel[1]}"
    return rel[0] if rel else ""


def _resolve_targets(tree: ast.AST, package: tuple[str, ...]) -> set[str]:
    """解析 import 目标，归一化为 gensokyoai 内模块键集合（外部依赖不产生键）"""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                cut = len(package) - (node.level - 1)
                if cut < 0:
                    continue  # 越出 gensokyoai 包外，外部依赖
                full = package[:cut] + tuple((node.module or "").split("."))
            else:
                full = tuple((node.module or "").split("."))
                if not full or full[0] != "gensokyoai":
                    continue
                full = full[1:]
            key = _module_key(full)
            if key:
                targets.add(key)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                full = tuple(alias.name.split("."))
                if full and full[0] == "gensokyoai":
                    key = _module_key(full[1:])
                    if key:
                        targets.add(key)
    return targets


def _py_files(module_dir: str) -> list[Path]:
    return [f for f in (GENSOKYOAI_DIR / module_dir).glob("*.py") if f.name != "__init__.py"]


def test_no_cross_business_imports():
    """业务子包之间禁止直接 import（含自身包内的两段式误判豁免）"""
    for module, forbidden in FORBIDDEN_CROSS_IMPORTS.items():
        for py_file in _py_files(module):
            own = _own_key(py_file)
            targets = _resolve_targets(
                ast.parse(py_file.read_text(encoding="utf-8")),
                _package_of(py_file),
            )
            bad = {t for t in targets if t in forbidden and t != own}
            assert not bad, f"❌ {py_file} 非法导入业务模块: {bad}"


def test_utils_is_leaf():
    """utils 是纯叶子，不依赖项目内任何模块"""
    for py_file in _py_files("utils"):
        targets = _resolve_targets(
            ast.parse(py_file.read_text(encoding="utf-8")),
            _package_of(py_file),
        )
        bad = targets - {"utils"}
        assert not bad, f"❌ {py_file} 依赖了项目内模块: {bad}"


def test_core_top_infra_only():
    """core 顶层基建不依赖业务子包 / models"""
    for py_file in _py_files("core"):
        own = _own_key(py_file)
        targets = _resolve_targets(
            ast.parse(py_file.read_text(encoding="utf-8")),
            _package_of(py_file),
        )
        bad = {t for t in targets if t in CORE_TOP_FORBIDDEN and t != own}
        assert not bad, f"❌ {py_file} 非法依赖: {bad}"

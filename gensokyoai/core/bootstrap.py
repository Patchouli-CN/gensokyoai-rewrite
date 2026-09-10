"""启动扫描：一行发现所有声明式扩展（架构文档 §5.3）"""

from .registry import auto_discover

DEFAULT_PACKAGES = (
    "gensokyoai.models",
    "gensokyoai.eyes",
    "gensokyoai.tools",
    "gensokyoai.core.brain",
    "gensokyoai.core.responder",
    "gensokyoai.core.memorizer",
    "gensokyoai.core.health",
)


def discover_all(packages: tuple[str, ...] = DEFAULT_PACKAGES) -> None:
    """扫描默认扩展包，触发装饰器注册"""
    for pkg in packages:
        auto_discover(pkg)

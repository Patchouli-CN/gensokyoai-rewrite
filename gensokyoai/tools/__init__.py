"""工具系统 —— 内置工具与工具编写约定。

内置工具用 `@ToolRegistry.tool` 声明注册；`bootstrap.discover_all()` 扫描本包时
自动注册，`TouhouWorld._setup_tools()` 启动即取，**业务代码不用管工具接线**。
"""

from .builtin import get_current_dateinfo, get_current_time, get_moon_phase

__all__ = [
    "get_current_dateinfo",
    "get_current_time",
    "get_moon_phase",
]

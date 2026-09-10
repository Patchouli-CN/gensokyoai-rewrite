"""内置工具 —— 角色扮演常用的一小撮「超能力」。

都是纯同步、零依赖、毫秒级的工具，用来验证工具链路并给角色一点外部感知能力。
新增工具只需在这里加 `@ToolRegistry.tool` 装饰的函数，重启即自动生效。
"""

import time
from datetime import datetime

from ..core.registry import ToolRegistry

_WEEKDAY_CN = ("月", "火", "水", "木", "金", "土", "日")
""" 七曜日（周一 -> 周日）"""

_MOON_PHASES = (
    "新月",
    "蛾眉月",
    "上弦月",
    "盈凸月",
    "满月",
    "亏凸月",
    "下弦月",
    "残月",
)
""" 八相月 """

_SYNODIC_MONTH = 29.530588853
""" 朔望月长度（天）"""

_NEW_MOON_EPOCH = 947182440.0
""" 参考新月时刻：2000-01-06 18:14 UTC（Unix 秒）"""


@ToolRegistry.tool
def get_current_time() -> str:
    """获取当前的日期与时间（本地时区）。

    Returns:
        str: 形如 "2026-09-09 21:30:15"
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@ToolRegistry.tool
def get_current_dateinfo() -> str:
    """获取今天的日期与曜日（七曜日）。

    Returns:
        str: 形如 "2026-09-09 星期三（水曜日）"
    """
    now = datetime.now()
    weekday = now.weekday()
    return f"{now.strftime('%Y-%m-%d')} 星期{_WEEKDAY_CN[weekday]}（{_WEEKDAY_CN[weekday]}曜日）"


@ToolRegistry.tool
def get_moon_phase() -> str:
    """查询当前的月相（八相月）。

    Returns:
        str: 形如 "盈凸月（月龄 10.3 天）"
    """
    elapsed = (time.time() - _NEW_MOON_EPOCH) % _SYNODIC_MONTH
    index = int(elapsed / _SYNODIC_MONTH * 8) % 8
    return f"{_MOON_PHASES[index]}（月龄 {elapsed:.1f} 天）"

"""内置工具 —— 角色扮演常用的一小撮「超能力」。

都是纯同步、零依赖、毫秒级的工具，用来验证工具链路并给角色一点外部感知能力。
新增工具只需在这里加 `@ToolRegistry.tool` 装饰的函数，重启即自动生效；
带参函数的参数 JSON Schema 从签名自动推导（`ToolSpec.to_openai_tool`）。
"""

import time
from datetime import date, datetime

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
    elapsed_days = ((time.time() - _NEW_MOON_EPOCH) / 86400) % _SYNODIC_MONTH
    index = int(elapsed_days / _SYNODIC_MONTH * 8) % 8
    return f"{_MOON_PHASES[index]}（月龄 {elapsed_days:.1f} 天）"


@ToolRegistry.tool
def days_until(target_date: str) -> str:
    """计算今天到目标日期还有多少天（节日倒计时用）。

    Args:
        target_date: 目标日期，格式 YYYY-MM-DD（如 "2026-12-22"）

    Returns:
        str: 形如 "距 2026-12-22 还有 91 天" / "2026-01-01 已经过去 263 天" / "就是今天"
    """
    try:
        target = datetime.strptime(target_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        return f"日期格式不对：{target_date!r}（应为 YYYY-MM-DD）"
    delta = (target - date.today()).days
    if delta > 0:
        return f"距 {target.isoformat()} 还有 {delta} 天"
    if delta < 0:
        return f"{target.isoformat()} 已经过去 {-delta} 天"
    return f"就是今天（{target.isoformat()}）"

"""get_moon_phase 回归测试：单位 bug（秒 % 天）修复后，锚定真实天文时刻校验"""

from datetime import UTC, datetime

from gensokyoai.tools import builtin
from gensokyoai.tools.builtin import get_moon_phase

# 天文实测锚点（timeanddate/space.com 可查）：2026-09-26 16:49 UTC 满月。
# 该时刻公式算出的月龄为 14.85（真值 14.77，误差 ~2 小时），其余测试时刻
# 从它按天数偏移，保证落在目标月相桶的中央而非边界。
_FULL_MOON_TS = datetime(2026, 9, 26, 16, 49, tzinfo=UTC).timestamp()


def test_full_moon_moment_reports_full(monkeypatch):
    """真实满月时刻：必须报满月（月龄 ~14.8 天）"""
    monkeypatch.setattr(builtin.time, "time", lambda: _FULL_MOON_TS)
    assert get_moon_phase().startswith("满月")


def test_new_moon_moment_reports_new(monkeypatch):
    """新月时刻（满月 - 半个朔望月）：必须报新月（月龄 ~0 天）"""
    monkeypatch.setattr(builtin.time, "time", lambda: _FULL_MOON_TS - 14.77 * 86400)
    assert get_moon_phase().startswith("新月")


def test_day_before_full_is_waxing_gibbous(monkeypatch):
    """满月前一天：盈凸月（2026 中秋节当天的真实月相）"""
    monkeypatch.setattr(builtin.time, "time", lambda: _FULL_MOON_TS - 86400)
    assert get_moon_phase().startswith("盈凸月")

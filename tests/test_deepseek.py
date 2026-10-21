"""DeepSeek 计费测试 —— 人民币、峰谷两档、缓存命中、节假日。"""

from datetime import datetime

import pytest

from gensokyoai.llm import BEIJING, Usage, UsageLedger
from gensokyoai.llm.deepseek import billing_for

MONDAY_PEAK = datetime(2024, 1, 1, 10, 0, tzinfo=BEIJING).timestamp()
""" 2024-01-01 是周一，10:00 落在高峰里 """

MONDAY_LUNCH = datetime(2024, 1, 1, 13, 0, tzinfo=BEIJING).timestamp()
""" 午休时段（12-14）不算高峰 """

SATURDAY = datetime(2024, 1, 6, 10, 0, tzinfo=BEIJING).timestamp()


@pytest.fixture
def flash():
    billing = billing_for("deepseek-flash")
    assert billing is not None
    return billing


def test_weekday_hours_are_peak(flash):
    """工作日 9-12、14-18 是高峰，午休和晚上不是"""
    assert flash.is_peak(MONDAY_PEAK)
    assert not flash.is_peak(MONDAY_LUNCH)
    assert not flash.is_peak(SATURDAY), "周末全天算空闲"


def test_holidays_are_off_peak():
    """法定节假日哪怕在工作日也按空闲价"""
    billing = billing_for("deepseek-flash", holidays=("2024-01-01",))
    assert billing is not None
    assert not billing.is_peak(MONDAY_PEAK)


def test_peak_costs_double(flash):
    """同一份用量，高峰是空闲的两倍"""
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    off = flash.cost(usage, MONDAY_LUNCH)
    peak = flash.cost(usage, MONDAY_PEAK)

    assert (round(off, 6), round(peak, 6)) == (5.0, 10.0), "空闲 1+4，高峰 2+8"


def test_cached_price_is_its_own_tier(flash):
    """缓存命中单独一档（0.02 / 0.04 元）"""
    cached_only = Usage(prompt_tokens=1_000_000, cached_tokens=1_000_000)

    assert round(flash.cost(cached_only, MONDAY_LUNCH), 6) == 0.02
    assert round(flash.cost(cached_only, MONDAY_PEAK), 6) == 0.04


def test_currency_and_description(flash):
    """人民币，摘要里说清两档"""
    assert flash.currency == "CNY"
    described = flash.describe()
    assert "高峰" in described and "空闲" in described and "9-12" in described


def test_ledger_picks_tier_by_clock(flash):
    """账本按记账时刻挑档"""
    ledger = UsageLedger(flash, clock=lambda: MONDAY_PEAK)
    usage = Usage(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    spent = ledger.record(usage)

    assert round(spent, 6) == 10.0
    assert ledger.currency == "CNY"
    assert "¥" in ledger.summary()


def test_legacy_model_name_is_billed_as_flash():
    """旧名 deepseek-v4-flash 按 flash 计费"""
    legacy = billing_for("deepseek-v4-flash")
    current = billing_for("deepseek-flash")

    assert legacy is not None and current is not None
    assert legacy.off == current.off and legacy.peak == current.peak


def test_pro_model_is_more_expensive():
    """v4-pro 比 flash 贵（且不支持看图，另说）"""
    pro = billing_for("deepseek-v4-pro")
    flash = billing_for("deepseek-flash")

    assert pro is not None and flash is not None
    assert pro.off.output == 13.5
    assert pro.peak.output == 27.0


def test_unknown_model_has_no_billing():
    """表里没有的模型：没有计费口径，只记 token"""
    assert billing_for("deepseek-unknown") is None

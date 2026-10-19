"""Hand-checked daily fixtures: causal levels, zone lifecycle and data contracts."""

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from sequoia_x.core.config import DailySignalConfig
from sequoia_x.strategy.daily_signals import analyze_daily_bars, turtle_breakout_mask


def bars(n=65):
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-02", periods=n).strftime("%Y-%m-%d"),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "volume": 100.0,
            "turnover": 200_000_000.0,
            "price_adjustment": "hfq",
        }
    )


def set_bar(df, i, o, h, lo, c, volume=100):
    df.loc[i, ["open", "high", "low", "close", "volume"]] = [o, h, lo, c, volume]


def test_false_breakdown_uses_prior_support_and_strict_reclaim():
    df = bars()
    set_bar(df, 64, 9.2, 10.0, 8.5, 9.5)
    result = analyze_daily_bars(df)[-1]
    assert result["levels"]["support"] == 9.0
    assert "failure_swing" in result["signals"]
    set_bar(df, 64, 9.2, 10.0, 9.0, 9.5)
    assert "failure_swing" not in analyze_daily_bars(df)[-1]["signals"]
    set_bar(df, 64, 9.2, 10.0, 8.5, 9.0)
    assert "failure_swing" not in analyze_daily_bars(df)[-1]["signals"]


def test_turtle_breakout_is_shared_and_requires_full_warmup():
    df = bars(21)
    set_bar(df, 20, 10.5, 12.0, 10.0, 11.5)
    assert turtle_breakout_mask(df).tolist() == [False] * 20 + [True]
    assert analyze_daily_bars(df)[-1]["signals"] == ["breakout_20"]
    df.loc[20, "turnover"] = 100_000_000
    assert not turtle_breakout_mask(df).iloc[-1]


def test_failed_breakout_uses_original_breakout_level():
    df = bars(23)
    set_bar(df, 21, 10.5, 12.0, 10.0, 11.5)
    set_bar(df, 22, 11.5, 11.6, 10.0, 11.0)
    result = analyze_daily_bars(df)[-1]
    assert "breakout_failure" in result["signals"]
    assert result["levels"]["failed_breakout_level"] == 11.0


def test_new_volume_zone_cannot_confirm_its_own_retest():
    df = bars(24)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 250)
    set_bar(df, 22, 11.8, 12.8, 11.5, 12.5)
    set_bar(df, 23, 12.0, 12.2, 9.0, 9.4)
    rows = analyze_daily_bars(df)
    assert "demand_zone_created" in rows[21]["signals"]
    assert "demand_retest" not in rows[21]["signals"]
    assert rows[21]["zones"][0]["low"] == 9.5
    assert rows[21]["zones"][0]["high"] == 12.0
    assert "demand_retest" in rows[22]["signals"]
    assert "defense_failure" in rows[23]["signals"]
    assert rows[23]["zones"] == []


def test_supply_rejection_and_close_invalidation_are_symmetric():
    df = bars(24)
    set_bar(df, 21, 10.0, 10.5, 8.0, 8.2, 250)
    set_bar(df, 22, 8.0, 8.5, 7.4, 7.8)
    set_bar(df, 23, 10.0, 11.0, 9.5, 10.8)
    rows = analyze_daily_bars(df)
    assert "supply_zone_created" in rows[21]["signals"]
    assert "supply_rejection" in rows[22]["signals"]
    assert "supply_zone_broken" in rows[23]["signals"]
    assert rows[23]["zones"] == []


def test_zones_expire_by_observed_trading_bars_and_do_not_reappear():
    df = bars(25)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 250)
    rows = analyze_daily_bars(df, DailySignalConfig(zone_max_age=1))
    assert rows[22]["zones"]
    assert rows[23]["zones"] == []
    assert rows[24]["zones"] == []


def test_volume_threshold_excludes_today_and_equality_does_not_qualify():
    df = bars(22)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 200)
    assert "demand_zone_created" not in analyze_daily_bars(df)[-1]["signals"]
    df.loc[21, "volume"] = 201
    assert "demand_zone_created" in analyze_daily_bars(df)[-1]["signals"]


def test_trend_continuation_requires_rising_averages_and_prior_high_reclaim():
    df = bars(65)
    for i in range(65):
        p = 10 + i * 0.1
        set_bar(df, i, p, p + 0.2, p - 0.2, p + 0.1)
    set_bar(df, 64, 16.4, 17.0, 16.2, 16.8)
    assert "trend_continuation" in analyze_daily_bars(df)[-1]["signals"]
    assert "trend_continuation" not in analyze_daily_bars(bars())[-1]["signals"]


@given(scale=st.floats(min_value=0.2, max_value=5, allow_nan=False, allow_infinity=False))
def test_consistent_price_scaling_preserves_signals(scale):
    df = bars(24)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 250)
    set_bar(df, 22, 11.8, 12.8, 11.5, 12.5)
    scaled = df.copy()
    scaled[["open", "high", "low", "close"]] *= scale
    assert [r["signals"] for r in analyze_daily_bars(df)] == [
        r["signals"] for r in analyze_daily_bars(scaled)
    ]


def test_every_prefix_and_future_mutations_preserve_historical_output():
    df = bars(70)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 250)
    set_bar(df, 22, 11.8, 12.8, 11.5, 12.5)
    complete = analyze_daily_bars(df)
    for n in range(1, len(df) + 1):
        assert analyze_daily_bars(df.iloc[:n]) == complete[:n]
    mutated = df.copy()
    mutated.loc[40:, ["open", "high", "low", "close", "volume"]] *= 4
    assert analyze_daily_bars(mutated)[:40] == complete[:40]


def test_zone_capacity_evicts_oldest_without_changing_old_snapshots():
    df = bars(24)
    set_bar(df, 21, 10.0, 12.0, 9.5, 11.8, 250)
    set_bar(df, 22, 10.0, 12.0, 9.5, 11.8, 300)
    rows = analyze_daily_bars(df, DailySignalConfig(max_active_zones=1))
    assert rows[21]["zones"][0]["created_date"] == df.iloc[21]["date"]
    assert rows[22]["zones"][0]["created_date"] == df.iloc[22]["date"]
    assert rows[22]["zone_events"][-1]["event"] == "capacity_evicted"


def test_feature_readiness_distinguishes_warmup_from_absent_signal():
    rows = analyze_daily_bars(bars())
    assert rows[19]["readiness"] == {
        "breakout_20": False,
        "failure_swing": False,
        "volume_zones": False,
        "trend_continuation": False,
    }
    assert rows[20]["readiness"] == {
        "breakout_20": True,
        "failure_swing": True,
        "volume_zones": True,
        "trend_continuation": False,
    }
    assert all(rows[60]["readiness"].values())


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "raw",
        "mixed",
        "nan",
        "bad_ohlc",
        "zero_volume",
        "negative_amount",
        "duplicate",
        "unsorted",
    ],
)
def test_bad_or_mixed_basis_data_fail_closed(change):
    df = bars()
    if change == "unknown":
        df = df.drop(columns="price_adjustment")
    elif change in {"raw", "mixed"}:
        df.loc[0 if change == "mixed" else slice(None), "price_adjustment"] = "raw"
    elif change == "nan":
        df.loc[5, "close"] = float("nan")
    elif change == "bad_ohlc":
        df.loc[5, "low"] = 15
    elif change == "zero_volume":
        df.loc[5, "volume"] = 0
    elif change == "negative_amount":
        df.loc[5, "turnover"] = -1
    elif change == "duplicate":
        df.loc[5, "date"] = df.loc[4, "date"]
    else:
        df = df.iloc[::-1]
    with pytest.raises(ValueError):
        analyze_daily_bars(df)


def test_input_is_not_mutated_and_incomplete_warmup_emits_no_event():
    df = bars(10)
    original = df.copy(deep=True)
    assert all(not r["signals"] for r in analyze_daily_bars(df))
    pd.testing.assert_frame_equal(df, original)


@pytest.mark.parametrize(
    "kwargs",
    [{"support_window": 0}, {"zone_volume_ratio": 1}, {"trend_fast": 60, "trend_slow": 20}],
)
def test_bad_configuration_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        DailySignalConfig(**kwargs)

"""Causal daily observations; independent rules inspired by Heavy91's concepts.

No orders, entry prices, returns, future pivots or shifted-back confirmations.
Every output row uses only that row and its preceding completed daily bars.
"""

import math

import pandas as pd

from sequoia_x.core.config import DailySignalConfig

SIGNAL_EXPLANATIONS = {
    "breakout_20": "收盘突破前20根日线最高价，阳线且高于昨收，成交额严格过亿；复用海龟条件。",
    "failure_swing": "最低价严格跌破前N根日线最低价，收盘重新站回；拒绝新低观察。",
    "trend_continuation": "快慢均线均上升且多头排列，阳线收盘越过昨日高点；趋势延续观察。",
    "demand_zone_created": "成交量超过此前均量阈值，阳线收在全幅上1/3；形成候选需求区。",
    "supply_zone_created": "成交量超过此前均量阈值，阴线收在全幅下1/3；形成候选供给区。",
    "demand_retest": "价格触及先前需求区，阳线收盘重回该区上沿之上。",
    "supply_rejection": "价格触及先前供给区，阴线收盘回到该区下沿之下。",
    "defense_failure": "收盘跌破一个尚有效的需求区下沿，该区即日失效。",
    "supply_zone_broken": "收盘突破一个尚有效的供给区上沿，该区即日失效。",
    "breakout_failure": "昨日满足海龟突破条件，今日收盘退回昨日突破时的原20日高点或以下。",
}


def turtle_breakout_mask(df: pd.DataFrame) -> pd.Series:
    """The existing Turtle predicate, shared without market-cap API calls."""
    prior_high = df["high"].shift(1).rolling(20, min_periods=20).max()
    return (
        (df["close"] > prior_high)
        & (df["turnover"] > 100_000_000)
        & (df["close"] > df["open"])
        & (df["close"] > df["close"].shift(1))
    ).fillna(False)


def _validated_bars(df: pd.DataFrame) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume", "turnover", "price_adjustment"]
    if not set(required).issubset(df.columns):
        raise ValueError("missing OHLCV/price_adjustment; rebuild a fresh hfq database")
    data = df[required].copy().reset_index(drop=True)
    if data.empty:
        return data
    if not data["price_adjustment"].eq("hfq").all():
        raise ValueError("unknown or mixed price_adjustment; rebuild a fresh hfq database")
    try:
        dates = pd.to_datetime(data["date"], format="%Y-%m-%d", errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid daily dates") from exc
    if dates.isna().any() or not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError("dates must be unique, non-null and increasing")
    data["date"] = dates.dt.strftime("%Y-%m-%d")
    numeric = ["open", "high", "low", "close", "volume", "turnover"]
    for column in numeric:
        data[column] = pd.to_numeric(data[column], errors="raise")
        if not data[column].map(math.isfinite).all():
            raise ValueError(f"non-finite {column}")
    if (data[["open", "high", "low", "close", "volume"]] <= 0).any().any():
        raise ValueError("non-positive OHLC/volume; suspended bars are not signal bars")
    if (data["turnover"] < 0).any():
        raise ValueError("negative traded amount")
    if (
        (data["low"] > data[["open", "close"]].min(axis=1))
        | (data["high"] < data[["open", "close"]].max(axis=1))
        | (data["high"] < data["low"])
    ).any():
        raise ValueError("inconsistent OHLC")
    return data


def _number(value) -> float | None:
    return None if pd.isna(value) else float(value)


def analyze_daily_bars(
    df: pd.DataFrame,
    config: DailySignalConfig | None = None,
) -> list[dict]:
    """Return immutable-by-convention daily snapshots on explicitly tagged hfq bars.

    Caller must provide completed bars only. The report entry point enforces a
    Shanghai close cutoff. Dates are not inferred to be exchange sessions.
    """
    cfg = config or DailySignalConfig()
    data = _validated_bars(df)
    if data.empty:
        return []
    support = data["low"].shift(1).rolling(cfg.support_window).min()
    high20 = data["high"].shift(1).rolling(20).max()
    volume_mean = data["volume"].shift(1).rolling(cfg.volume_window).mean()
    fast = data["close"].rolling(cfg.trend_fast).mean()
    slow = data["close"].rolling(cfg.trend_slow).mean()
    breakouts = turtle_breakout_mask(data)
    active: list[dict] = []
    output: list[dict] = []

    for i, row in enumerate(data.itertuples(index=False)):
        signals: list[str] = []
        zone_events: list[dict] = []
        kept: list[dict] = []
        # Evaluate only zones established at an earlier close. Invalidation wins
        # over a wick retest; expired zones cannot signal or reappear later.
        for zone in active:
            if i - zone["created_index"] > cfg.zone_max_age:
                zone_events.append({**zone, "event": "expired"})
                continue
            demand = zone["kind"] == "demand"
            broken = row.close < zone["low"] if demand else row.close > zone["high"]
            if broken:
                signals.append("defense_failure" if demand else "supply_zone_broken")
                zone_events.append({**zone, "event": "invalidated"})
                continue
            overlaps = row.low <= zone["high"] and row.high >= zone["low"]
            if overlaps:
                if demand and row.close > zone["high"] and row.close > row.open:
                    signals.append("demand_retest")
                    zone_events.append({**zone, "event": "retest"})
                elif not demand and row.close < zone["low"] and row.close < row.open:
                    signals.append("supply_rejection")
                    zone_events.append({**zone, "event": "rejection"})
            kept.append(zone)
        active = kept
        if breakouts.iloc[i]:
            signals.append("breakout_20")
        if pd.notna(support.iloc[i]) and row.low < support.iloc[i] < row.close:
            signals.append("failure_swing")
        if (
            i > 0
            and pd.notna(slow.iloc[i - 1])
            and row.close > fast.iloc[i] > slow.iloc[i]
            and fast.iloc[i] > fast.iloc[i - 1]
            and slow.iloc[i] > slow.iloc[i - 1]
            and row.close > data.iloc[i - 1]["high"]
            and row.close > row.open
        ):
            signals.append("trend_continuation")
        failed_level = None
        if i > 0 and breakouts.iloc[i - 1] and row.close <= high20.iloc[i - 1]:
            signals.append("breakout_failure")
            failed_level = float(high20.iloc[i - 1])
        baseline = volume_mean.iloc[i]
        if pd.notna(baseline) and row.volume > baseline * cfg.zone_volume_ratio:
            span = row.high - row.low
            location = (row.close - row.low) / span if span > 0 else 0.5
            kind = None
            if row.close > row.open and location >= 2 / 3:
                kind = "demand"
            elif row.close < row.open and location <= 1 / 3:
                kind = "supply"
            if kind:
                zone = {
                    "kind": kind,
                    "low": float(row.low),
                    "high": float(row.high),
                    "created_date": row.date,
                    "created_index": i,
                    "volume_ratio": float(row.volume / baseline),
                }
                active.append(zone)
                signals.append(f"{kind}_zone_created")
                zone_events.append({**zone, "event": "created"})
                if len(active) > cfg.max_active_zones:
                    removed = active.pop(0)
                    zone_events.append({**removed, "event": "capacity_evicted"})
        unique_signals = list(dict.fromkeys(signals))
        output.append(
            {
                "date": row.date,
                "available_after": f"{row.date}T15:00:00+08:00",
                "price_adjustment": "hfq",
                "status": "OBSERVE_ONLY",
                "signals": unique_signals,
                "explanations": {key: SIGNAL_EXPLANATIONS[key] for key in unique_signals},
                "levels": {
                    "support": _number(support.iloc[i]),
                    "breakout_20": _number(high20.iloc[i]),
                    "failed_breakout_level": failed_level,
                },
                "volume_ratio": float(row.volume / baseline) if pd.notna(baseline) else None,
                "trend_ready": bool(i >= cfg.trend_slow),
                "readiness": {
                    "breakout_20": i >= 20,
                    "failure_swing": i >= cfg.support_window,
                    "volume_zones": i >= cfg.volume_window,
                    "trend_continuation": i >= cfg.trend_slow,
                },
                "zones": [dict(zone) for zone in active],
                "zone_events": zone_events,
            }
        )
    return output

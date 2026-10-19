"""Completed-close, local-database daily signal reporting without notifications."""

import json
import sqlite3
from datetime import date
from pathlib import Path

from sequoia_x.core.config import DailySignalConfig
from sequoia_x.core.daily_time import resolve_as_of as resolve_as_of
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.daily_signals import analyze_daily_bars


def scan_daily_observations(
    engine: DataEngine,
    config: DailySignalConfig,
    as_of: str,
) -> dict:
    """Analyze only the universe present at cutoff; bad/stale series fail closed.

    `as_of` is a completed-close date from resolve_as_of at CLI boundaries.
    Pure historical callers may supply their own known completed date.
    """
    if date.fromisoformat(as_of).isoformat() != as_of:
        raise ValueError("as_of must be an ISO date")
    db_uri = Path(engine.db_path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        universe = conn.execute(
            "SELECT symbol, MAX(date) FROM stock_daily WHERE date <= ? "
            "GROUP BY symbol ORDER BY symbol",
            (as_of,),
        ).fetchall()
    observations: list[dict] = []
    excluded: list[dict] = []
    dated_universe = []
    for symbol, last_date in universe:
        try:
            if date.fromisoformat(last_date).isoformat() != last_date:
                raise ValueError("non-canonical daily date")
        except (ValueError, TypeError):
            excluded.append(
                {"symbol": symbol, "reason": "INVALID_DATA", "detail": "invalid latest daily date"}
            )
            continue
        dated_universe.append((symbol, last_date))
    latest = max((row[1] for row in dated_universe), default=None)
    evaluated = 0
    for symbol, last_date in dated_universe:
        if last_date != latest:
            excluded.append({"symbol": symbol, "reason": "STALE_SYMBOL", "last_date": last_date})
            continue
        try:
            data = engine.get_ohlcv(symbol, as_of=as_of)
            rows = analyze_daily_bars(data, config)
        except (ValueError, TypeError) as exc:
            excluded.append({"symbol": symbol, "reason": "INVALID_DATA", "detail": str(exc)})
            continue
        if not rows:
            excluded.append({"symbol": symbol, "reason": "NO_VALID_BARS"})
            continue
        last = rows[-1]
        if not any(last["readiness"].values()):
            excluded.append({"symbol": symbol, "reason": "INSUFFICIENT_HISTORY"})
            continue
        evaluated += 1
        if last["signals"] or last["zones"]:
            observations.append({"symbol": symbol, **last})
    return {
        "schema_version": 1,
        "status": "OBSERVE_ONLY",
        "trade_eligible": False,
        "as_of": as_of,
        "latest_data_date": latest,
        "as_of_gap_calendar_days": (date.fromisoformat(as_of) - date.fromisoformat(latest)).days
        if latest
        else None,
        "coverage": "LOCAL_DATABASE_ONLY",
        "calendar_verified": False,
        "local_symbols": len(universe),
        "evaluated_symbols": evaluated,
        "excluded": excluded,
        "observations": observations,
        "config": config.model_dump(),
        "execution_policy": {
            "signal_time": "COMPLETED_DAILY_CLOSE_ASIA_SHANGHAI",
            "min_entry_session_offset": 1,
            "min_exit_session_offset_from_actual_entry": 1,
            "entry_or_fill_assumed": False,
            "constraints_unmodeled": [
                "exchange_calendar",
                "suspensions",
                "price_limits",
                "liquidity",
                "slippage",
                "fees",
                "position_inventory",
            ],
        },
        "limitations": [
            "观察信号，不生成订单、成交价格、目标收益或胜率。",
            "后复权价位为研究坐标，不能直接作为真实委托价。",
            "供需区为量价假设，不证明机构持仓或净资金流。",
            "仅扫描本地数据库；缺失、滞后或空结果不代表全市场无机会或空仓结论。",
            "未验证交易日历、实际成交、跨日运行及策略收益。",
        ],
    }


def write_daily_report(report: dict, path: str) -> None:
    """Write standard JSON, rejecting any accidental NaN/Infinity payload."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )

"""Offline reports must enforce completed close cutoffs and never notify."""

import json
import sqlite3
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import main as main_module
from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine
from sequoia_x.research.daily_report import resolve_as_of, scan_daily_observations
from tests.test_daily_signals import bars, set_bar

SH = ZoneInfo("Asia/Shanghai")


def seeded_engine(tmp_path):
    settings = Settings(db_path=str(tmp_path / "fixture.db"), feishu_webhook_url="", _env_file=None)
    engine = DataEngine(settings)
    df = bars(23)
    set_bar(df, 21, 10.5, 12.0, 10.0, 11.5)
    set_bar(df, 22, 11.5, 11.6, 10.0, 11.0)
    df["symbol"] = "000001"
    with sqlite3.connect(engine.db_path) as conn:
        df.to_sql("stock_daily", conn, if_exists="append", index=False)
    return engine, settings, df


def test_cutoff_is_shanghai_close_and_rejects_future_or_unclosed_explicit_date():
    morning = datetime(2026, 10, 19, 10, tzinfo=SH)
    assert resolve_as_of(None, morning) == "2026-10-18"
    assert resolve_as_of(None, morning.astimezone(ZoneInfo("UTC"))) == "2026-10-18"
    assert resolve_as_of(None, datetime(2026, 10, 19, 15, tzinfo=SH)) == "2026-10-19"
    for invalid in ["2026-10-19", "2026-10-20", "2026-02-30", "20261018"]:
        with pytest.raises(ValueError):
            resolve_as_of(invalid, morning)


def test_report_only_uses_cutoff_bars_and_does_not_guess_entry_dates(tmp_path):
    engine, settings, df = seeded_engine(tmp_path)
    report = scan_daily_observations(engine, settings.daily_signals, df.iloc[21]["date"])
    assert report["evaluated_symbols"] == 1
    row = report["observations"][0]
    assert row["date"] == df.iloc[21]["date"]
    assert row["signals"] == ["breakout_20"]
    assert row["symbol"] == "000001"
    assert report["trade_eligible"] is False
    assert report["execution_policy"]["min_entry_session_offset"] == 1
    assert report["execution_policy"]["min_exit_session_offset_from_actual_entry"] == 1
    assert report["coverage"] == "LOCAL_DATABASE_ONLY"
    assert report["calendar_verified"] is False
    json.dumps(report, allow_nan=False)


def test_unknown_basis_and_stale_symbols_are_excluded_with_reasons(tmp_path):
    engine, settings, df = seeded_engine(tmp_path)
    unknown = df.copy()
    unknown["symbol"] = "000002"
    unknown["price_adjustment"] = None
    stale = df.iloc[:21].copy()
    stale["symbol"] = "000003"
    future = df.copy()
    future["symbol"] = "000004"
    future["date"] = "2099-01-01"
    with sqlite3.connect(engine.db_path) as conn:
        unknown.to_sql("stock_daily", conn, if_exists="append", index=False)
        stale.to_sql("stock_daily", conn, if_exists="append", index=False)
        future.iloc[:1].to_sql("stock_daily", conn, if_exists="append", index=False)
    report = scan_daily_observations(engine, settings.daily_signals, df.iloc[-1]["date"])
    assert report["local_symbols"] == 3  # future-only listing cannot leak into historical universe
    assert report["evaluated_symbols"] == 1
    assert {r["symbol"] for r in report["excluded"]} == {"000002", "000003"}
    assert {r["reason"] for r in report["excluded"]} == {"INVALID_DATA", "STALE_SYMBOL"}


def test_local_cli_writes_real_report_without_sync_notification_or_webhook(tmp_path, monkeypatch):
    engine, _, df = seeded_engine(tmp_path)
    target = tmp_path / "report.json"
    monkeypatch.setenv("DB_PATH", engine.db_path)
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--observe-only", "--as-of", df.iloc[21]["date"], "--output", str(target)],
    )
    with (
        patch.object(DataEngine, "sync_today_bulk", side_effect=AssertionError("network")),
        patch.object(main_module, "FeishuNotifier", side_effect=AssertionError("notify")),
    ):
        main_module.main()
    report = json.loads(target.read_text())
    assert report["observations"][0]["signals"] == ["breakout_20"]
    assert "webhook" not in target.read_text().lower()


def test_main_daily_flow_generates_observation_report(tmp_path, monkeypatch):
    engine, settings, _ = seeded_engine(tmp_path)
    monkeypatch.setattr("sys.argv", ["main.py", "--output", str(tmp_path / "daily.json")])
    with (
        patch.object(main_module, "get_settings", return_value=settings),
        patch.object(DataEngine, "sync_today_bulk", return_value=0),
        patch.object(main_module, "build_strategies", return_value=[]),
    ):
        main_module.main()
    result = json.loads((tmp_path / "daily.json").read_text())
    assert result["observations"][0]["signals"] == ["breakout_failure"]


def test_nested_env_configuration_is_used(monkeypatch):
    monkeypatch.setenv("DAILY_SIGNALS__ZONE_VOLUME_RATIO", "3")
    cfg = Settings(feishu_webhook_url="", _env_file=None).daily_signals
    assert cfg.zone_volume_ratio == 3


def test_insufficient_history_is_not_counted_as_a_valid_negative_scan(tmp_path):
    engine, settings, df = seeded_engine(tmp_path)
    with sqlite3.connect(engine.db_path) as conn:
        conn.execute("DELETE FROM stock_daily WHERE date > ?", (df.iloc[9]["date"],))
    report = scan_daily_observations(engine, settings.daily_signals, df.iloc[9]["date"])
    assert report["evaluated_symbols"] == 0
    assert report["excluded"][0]["reason"] == "INSUFFICIENT_HISTORY"


def test_malformed_symbol_date_cannot_poison_healthy_market_alignment(tmp_path):
    engine, settings, df = seeded_engine(tmp_path)
    malformed = df.iloc[-1:].copy()
    malformed["symbol"] = "000002"
    malformed["date"] = "2025-11-zz"
    with sqlite3.connect(engine.db_path) as conn:
        malformed.to_sql("stock_daily", conn, if_exists="append", index=False)
    report = scan_daily_observations(engine, settings.daily_signals, "2025-12-31")
    assert report["latest_data_date"] == df.iloc[-1]["date"]
    assert report["evaluated_symbols"] == 1
    assert report["observations"][0]["symbol"] == "000001"
    assert report["excluded"] == [
        {"symbol": "000002", "reason": "INVALID_DATA", "detail": "invalid latest daily date"}
    ]

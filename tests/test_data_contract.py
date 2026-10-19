"""Offline checks for stored adjustment provenance and incremental history safety."""

import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from sequoia_x.core.config import Settings
from sequoia_x.core.daily_time import SHANGHAI, resolve_as_of
from sequoia_x.data.engine import DataEngine


def make_engine(tmp_path):
    return DataEngine(
        Settings(
            db_path=str(tmp_path / "daily.db"),
            start_date="2024-01-01",
            feishu_webhook_url="https://example.com/test-only",
            _env_file=None,
        )
    )


@pytest.fixture
def preclose_cutoff(monkeypatch):
    """At Shanghai 10:00 Jan 4, Jan 3 is the latest completed calendar date."""
    frozen_now = datetime(2024, 1, 4, 10, tzinfo=SHANGHAI)
    monkeypatch.setattr(
        "sequoia_x.data.engine.resolve_as_of",
        lambda value: resolve_as_of(value, now=frozen_now),
        raising=False,
    )


def run_ingestion(engine, mode, monkeypatch):
    if mode == "sync":
        monkeypatch.setattr("multiprocessing.Pool", InlinePool)
        return engine.sync_today_bulk()
    engine.backfill(["600000"])
    return None


def insert_rows(engine, rows):
    with sqlite3.connect(engine.db_path) as conn:
        pd.DataFrame(rows).to_sql("stock_daily", conn, if_exists="append", index=False)


def bar(symbol, trade_date, close=10.0):
    return {
        "symbol": symbol,
        "date": trade_date,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000.0,
        "turnover": close * 1000,
    }


class ProviderRows:
    fields = ["date", "open", "high", "low", "close", "volume", "amount"]
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self.rows = iter(rows)
        self.current = None

    def next(self):
        self.current = next(self.rows, None)
        return self.current is not None

    def get_row_data(self):
        return self.current


def mock_provider(monkeypatch, rows_by_code):
    queries = []

    def query(code, fields, **kwargs):
        queries.append((code, fields, kwargs))
        return ProviderRows(rows_by_code.get(code, []))

    monkeypatch.setitem(
        sys.modules,
        "baostock",
        SimpleNamespace(
            login=lambda: SimpleNamespace(error_code="0", error_msg=""),
            logout=lambda: None,
            query_history_k_data_plus=query,
        ),
    )
    return queries


class InlinePool:
    """Run provider workers locally so tests never spawn network-capable processes."""

    def __init__(self, workers):
        self.workers = workers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def map(self, function, chunks):
        return [function(chunk) for chunk in chunks]


def test_legacy_schema_migration_preserves_unknown_adjustment(tmp_path):
    db_path = tmp_path / "daily.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE stock_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL,
                volume REAL, turnover REAL, UNIQUE(symbol, date)
            )
        """)
        conn.execute(
            "INSERT INTO stock_daily (symbol, date, close) VALUES (?, ?, ?)",
            ("600000", "2024-01-02", 10.0),
        )

    engine = make_engine(tmp_path)
    legacy = engine.get_ohlcv("600000")
    assert "price_adjustment" in legacy.columns
    assert pd.isna(legacy.iloc[0]["price_adjustment"])
    assert legacy.iloc[0]["close"] == 10.0
    # Opening a migrated database again must not reinterpret old rows.
    assert pd.isna(make_engine(tmp_path).get_ohlcv("600000").iloc[0]["price_adjustment"])


def test_get_ohlcv_as_of_uses_inclusive_bound_cutoff(tmp_path):
    engine = make_engine(tmp_path)
    insert_rows(
        engine,
        [
            bar("600000", "2024-01-02"),
            bar("600000", "2024-01-03"),
            bar("600000", "2024-01-04"),
            bar("000001", "2024-01-02"),
        ],
    )

    history = engine.get_ohlcv("600000", as_of="2024-01-03")
    assert history["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert engine.get_ohlcv("600000' OR 1=1 --", as_of="2024-01-03").empty
    assert engine.get_ohlcv("600000", as_of="2024-01-01' OR 1=1 --").empty
    assert len(engine.get_ohlcv("600000")) == 3


def test_backfill_tags_only_new_hfq_provider_rows(tmp_path, monkeypatch, preclose_cutoff):
    engine = make_engine(tmp_path)
    insert_rows(engine, [bar("600000", "2024-01-02")])
    queries = mock_provider(
        monkeypatch,
        {
            "sh.600000": [["2024-01-03", "10", "11", "9", "10.5", "1000", "10500"]],
        },
    )

    engine.backfill(["600000"])

    history = engine.get_ohlcv("600000")
    assert pd.isna(history.iloc[0]["price_adjustment"])
    assert history.iloc[1]["price_adjustment"] == "hfq"
    assert queries[0][2]["adjustflag"] == "1"
    assert queries[0][2]["start_date"] == "2024-01-03"


def test_partial_incremental_sync_preserves_other_symbol_same_day(
    tmp_path, monkeypatch, preclose_cutoff
):
    engine = make_engine(tmp_path)
    latest_closed = "2024-01-03"
    preceding_day = "2024-01-02"
    insert_rows(engine, [bar("600000", preceding_day), bar("000001", latest_closed, close=20)])
    queries = mock_provider(
        monkeypatch,
        {
            "sh.600000": [[latest_closed, "10", "11", "9", "10.5", "1000", "10500"]],
        },
    )
    monkeypatch.setattr("multiprocessing.Pool", InlinePool)

    assert engine.sync_today_bulk() == 1

    unchanged = engine.get_ohlcv("000001")
    assert unchanged["close"].tolist() == [20.0]
    assert pd.isna(unchanged.iloc[0]["price_adjustment"])
    added = engine.get_ohlcv("600000").iloc[-1]
    assert added["date"] == latest_closed
    assert added["price_adjustment"] == "hfq"
    assert queries[0][2]["adjustflag"] == "1"


@pytest.mark.parametrize("mode", ["sync", "backfill"])
def test_ingestion_preclose_cutoff_filters_provider_unfinished_day(
    tmp_path, monkeypatch, preclose_cutoff, mode
):
    engine = make_engine(tmp_path)
    insert_rows(engine, [bar("600000", "2024-01-02")])
    queries = mock_provider(
        monkeypatch,
        {
            "sh.600000": [
                ["2024-01-03", "10", "11", "9", "10.5", "1000", "10500"],
                # Deliberate provider overflow: this bar is not yet complete.
                ["2024-01-04", "10", "12", "9", "11", "500", "5500"],
                ["2024-01-05", "10", "12", "9", "11", "500", "5500"],
            ],
        },
    )

    count = run_ingestion(engine, mode, monkeypatch)

    assert queries[0][2]["start_date"] == "2024-01-03"
    assert queries[0][2]["end_date"] == "2024-01-03"
    history = engine.get_ohlcv("600000")
    assert history["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert pd.isna(history.iloc[0]["price_adjustment"])
    assert history.iloc[1]["price_adjustment"] == "hfq"
    if mode == "sync":
        assert count == 1


@pytest.mark.parametrize("mode", ["sync", "backfill"])
def test_ingestion_repeated_run_refreshes_latest_completed_day(
    tmp_path, monkeypatch, preclose_cutoff, mode
):
    engine = make_engine(tmp_path)
    insert_rows(engine, [bar("600000", "2024-01-02"), bar("600000", "2024-01-03", close=8)])
    provider_rows = {
        "sh.600000": [["2024-01-03", "10", "11", "9", "10.5", "1000", "10500"]],
    }
    queries = mock_provider(monkeypatch, provider_rows)

    run_ingestion(engine, mode, monkeypatch)
    assert engine.get_ohlcv("600000").iloc[-1]["close"] == 10.5
    # A later provider revision must update the same row instead of skip or append failure.
    provider_rows["sh.600000"] = [["2024-01-03", "10", "12", "9", "11.5", "1000", "11500"]]
    run_ingestion(engine, mode, monkeypatch)

    assert len(queries) == 2
    assert all(query[2]["start_date"] == "2024-01-03" for query in queries)
    assert all(query[2]["end_date"] == "2024-01-03" for query in queries)
    history = engine.get_ohlcv("600000")
    assert len(history) == 2
    assert history.iloc[-1]["close"] == 11.5
    assert history.iloc[-1]["price_adjustment"] == "hfq"
    assert pd.isna(history.iloc[0]["price_adjustment"])


@pytest.mark.parametrize("mode", ["sync", "backfill"])
def test_ingestion_future_legacy_row_does_not_block_completed_day_refresh(
    tmp_path, monkeypatch, preclose_cutoff, mode
):
    engine = make_engine(tmp_path)
    insert_rows(engine, [bar("600000", "2024-01-02"), bar("600000", "2024-01-04", close=8)])
    queries = mock_provider(
        monkeypatch,
        {
            "sh.600000": [["2024-01-03", "10", "11", "9", "10.5", "1000", "10500"]],
        },
    )

    run_ingestion(engine, mode, monkeypatch)

    assert queries[0][2]["start_date"] == "2024-01-03"
    assert queries[0][2]["end_date"] == "2024-01-03"
    bounded = engine.get_ohlcv("600000", as_of="2024-01-03")
    assert bounded.iloc[-1]["date"] == "2024-01-03"
    assert bounded.iloc[-1]["price_adjustment"] == "hfq"
    assert pd.isna(engine.get_ohlcv("600000").iloc[-1]["price_adjustment"])

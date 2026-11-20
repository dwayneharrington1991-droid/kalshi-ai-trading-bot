"""Regression: a cycle with zero new eligible settlements must say whether settlements arrived and were ineligible."""
import sqlite3
from datetime import timedelta
from pathlib import Path

from src.signals_display import model_lifecycle as lc
from tests.test_signals_opening_models import NOW


def _journal(path: Path, observed_after_cursor: list[tuple[str, str]]):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE btc_engine_opening (mode TEXT,ticker TEXT,generated_at TEXT,cutoff TEXT,"
               "closes_at TEXT,up REAL,status TEXT,payload TEXT,PRIMARY KEY(mode,ticker))")
    db.execute("CREATE TABLE forecast_outcomes (ticker TEXT,origin TEXT,observed_at TEXT,outcome INTEGER,"
               "PRIMARY KEY(ticker,origin))")
    for ticker, observed in observed_after_cursor:
        # Official settlement with no opening record: ineligible, so it cannot be trained on.
        db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", (ticker, "official", observed, 1))
    db.commit()
    db.close()


def test_ineligible_new_settlement_is_reported_not_silently_counted_as_zero(tmp_path):
    cursor = NOW - timedelta(minutes=30)
    journal = tmp_path / "journal.db"
    _journal(journal, [("KXBTC15M-NEW1", (NOW - timedelta(minutes=10)).isoformat())])
    status = lc.retrain_cycle(journal, tmp_path / "state", NOW, "test", last_cutoff=cursor)
    assert status["decision"] == "SKIPPED_NOT_ENOUGH_NEW_DATA"
    assert status["new_settled_markets"] == 0
    assert status["new_official_settlements"] == 1
    assert status["new_by_reason"] == {"NO_FROZEN_OPENING_RECORD": 1}


def test_settlements_before_the_cursor_are_not_reported_as_new(tmp_path):
    cursor = NOW - timedelta(minutes=30)
    journal = tmp_path / "journal.db"
    _journal(journal, [("KXBTC15M-OLD1", (NOW - timedelta(hours=2)).isoformat())])
    status = lc.retrain_cycle(journal, tmp_path / "state", NOW, "test", last_cutoff=cursor)
    assert status["new_official_settlements"] == 0
    assert status["new_by_reason"] == {}

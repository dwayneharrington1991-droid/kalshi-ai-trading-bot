import sqlite3
import shutil

import pytest

from src.orders.local_position_repair import apply_plan, build_plan, local_candidates
from src.utils.database import DatabaseManager


def database(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY, market_id TEXT, side TEXT, quantity REAL,
                open_quantity REAL, status TEXT, live INTEGER, timestamp TEXT,
                last_reconciled_at TEXT, reconciliation_status TEXT
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY, position_id INTEGER, action TEXT, state TEXT,
                exchange_order_id TEXT, filled_quantity REAL
            );
            CREATE TABLE reconciliation_alerts (
                id INTEGER PRIMARY KEY, severity TEXT, kind TEXT, market_id TEXT,
                resolved_at TEXT
            );
            INSERT INTO positions VALUES
                (7, 'TEST-A', 'YES', 2, 2, 'open', 1, '2025-01-01', NULL, NULL);
            INSERT INTO reconciliation_alerts VALUES
                (1, 'critical', 'position_mismatch_yes', 'TEST-A', NULL);
        """)
        db.commit()


def test_plan_identifies_and_classifies_exchange_zero_position(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    candidates = local_candidates(path, {})
    plan = build_plan(path, candidates, {"TEST-A": {"market": {"status": "settled", "result": "yes"}}})
    assert [(row["position_id"], row["classification"]) for row in plan["repairs"]] == [(7, "settled")]
    assert plan["repairs"][0]["changes"]["status"] == {"before": "open", "after": "closed"}


def test_apply_is_local_transactional_audited_and_idempotent(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(path, local_candidates(path, {}), {"TEST-A": {"market": {"status": "closed"}}})
    assert apply_plan(path, plan) == 1
    assert apply_plan(path, plan) == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status, open_quantity FROM positions WHERE id=7").fetchone() == ("closed", 0.0)
        assert db.execute("SELECT COUNT(*) FROM local_reconciliation_repair_audit").fetchone()[0] == 1
        assert db.execute("SELECT resolved_at IS NOT NULL FROM reconciliation_alerts").fetchone()[0] == 1


def test_apply_rejects_position_changed_after_preview(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(path, local_candidates(path, {}), {"TEST-A": {}})
    with sqlite3.connect(path) as db:
        db.execute("UPDATE positions SET open_quantity=1 WHERE id=7")
        db.commit()
    with pytest.raises(RuntimeError, match="changed after preview"):
        apply_plan(path, plan)


@pytest.mark.asyncio
async def test_legacy_source_is_initialized_only_on_disposable_copy(tmp_path):
    source = tmp_path / "legacy.db"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL,
                side TEXT NOT NULL, entry_price REAL NOT NULL, quantity INTEGER NOT NULL,
                timestamp TEXT NOT NULL, rationale TEXT, confidence REAL,
                live BOOLEAN NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
                UNIQUE(market_id, side)
            );
            CREATE TABLE trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL,
                side TEXT NOT NULL, entry_price REAL NOT NULL, exit_price REAL NOT NULL,
                quantity INTEGER NOT NULL, pnl REAL NOT NULL,
                entry_timestamp TEXT NOT NULL, exit_timestamp TEXT NOT NULL, rationale TEXT
            );
            INSERT INTO positions
                (market_id, side, entry_price, quantity, timestamp, live, status)
            VALUES ('LEGACY', 'YES', .4, 2, '2026-01-01', 1, 'open');
        """)
        db.commit()
    original = source.read_bytes()
    working = tmp_path / "working.db"
    shutil.copy2(source, working)
    await DatabaseManager(str(working)).initialize()
    with sqlite3.connect(working) as db:
        db.execute("""
            INSERT INTO reconciliation_alerts
                (severity, kind, market_id, expected_value, observed_value,
                 first_seen_at, last_seen_at)
            VALUES ('critical', 'position_mismatch_yes', 'LEGACY', '2', '0', 'now', 'now')
        """)
        db.commit()
    assert [row["id"] for row in local_candidates(working, {})] == [1]
    assert source.read_bytes() == original

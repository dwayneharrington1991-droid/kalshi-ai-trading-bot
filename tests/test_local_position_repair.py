import sqlite3
import shutil
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import repair_stale_positions
from scripts.repair_stale_positions import (
    REQUIRED_REPORT_KEYS, assert_plan_unchanged, backup, complete_report,
)
from scripts.read_only_validate import ReadOnlyValidationError
from src.orders.local_position_repair import (
    apply_plan, build_plan, local_candidates, remote_position_map,
    build_account_activity_repairs, apply_account_activity_repairs,
)
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
    assert plan["repairs"][0]["changes"]["status"] == {
        "before": "open", "after": "administratively_reconciled"
    }


def test_apply_is_local_transactional_audited_and_idempotent(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}),
        {"TEST-A": {"market": {"status": "closed", "result": "yes"}}},
    )
    assert apply_plan(path, plan) == 1
    assert apply_plan(path, plan) == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status, open_quantity FROM positions WHERE id=7").fetchone() == (
            "administratively_reconciled", 0.0
        )
        assert db.execute("SELECT COUNT(*) FROM local_reconciliation_repair_audit").fetchone()[0] == 1
        assert db.execute("SELECT resolved_at IS NOT NULL FROM reconciliation_alerts").fetchone()[0] == 1


def test_apply_rejects_position_changed_after_preview(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}}
    )
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


def test_resting_order_blocks_repair(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}},
        active_orders=[{
            "order_id": "resting-1", "market_id": "TEST-A", "side": "YES",
            "status": "resting",
        }],
    )
    assert plan["repairs"] == []
    assert "active order" in plan["manual_review"][0]["manual_review_reasons"][0]


@pytest.mark.parametrize("response", [
    None, {}, {"market_positions": None}, {"market_positions": "bad"},
    {"market_positions": [{}]}, {"market_positions": [{"ticker": "A"}]},
    {"market_positions": [], "positions": []},
    {"market_positions": [], "cursor": "next-page"},
])
def test_malformed_positions_response_fails_closed(response):
    with pytest.raises(ValueError):
        remote_position_map(response)


def test_active_market_remains_manual_review(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "active"}}
    )
    assert plan["repairs"] == []
    assert plan["manual_review"][0]["classification"] == "manual_review"


def test_exact_paper_simulation_alert_is_preview_repair_only(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text(
        "live_mode=False for market TEST-A\n"
        "PAPER TRADE SIMULATED for TEST-A - No real money used\n",
        encoding="utf-8",
    )
    plan = build_plan(
        path, local_candidates(path, {}),
        {"TEST-A": {"market": {"ticker": "TEST-A", "status": "active"}}},
        preview_paper_simulation_alert_ids=[1], log_dir=logs,
    )
    assert plan["manual_review"] == []
    repair = plan["repairs"][0]
    assert repair["classification"] == "paper_simulation"
    assert repair["alert_ids"] == [1]
    assert repair["changes"]["reconciliation_status"]["after"] == (
        "administratively_reconciled_paper_simulation"
    )
    assert repair["position_before"]["live"] == 1


def test_paper_simulation_preview_fails_closed_on_exchange_history(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run.log").write_text(
        "live_mode=False for market TEST-A\nPAPER TRADE SIMULATED for TEST-A\n",
        encoding="utf-8",
    )
    exchange_order = SimpleNamespace(
        market_id="TEST-A", order_id="exchange-1", client_order_id="client-1",
        status="filled",
    )
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"ticker": "TEST-A", "status": "active"}},
        exchange_orders=[exchange_order], preview_paper_simulation_alert_ids=[1], log_dir=logs,
    )
    assert plan["repairs"] == []
    assert "exchange order history" in " ".join(plan["manual_review"][0]["manual_review_reasons"])


def test_duplicate_local_positions_require_manual_review(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO positions VALUES (8,'TEST-A','YES',1,1,'open',1,'2025',NULL,NULL)"
        )
        db.commit()
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}}
    )
    assert plan["repairs"] == []
    assert len(plan["manual_review"]) == 2


@pytest.mark.parametrize("column,value", [
    ("quantity", 3), ("live", 0), ("side", "NO"),
])
def test_changed_quantity_live_or_side_aborts(tmp_path, column, value):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}}
    )
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE positions SET {column}=? WHERE id=7", (value,))
        db.commit()
    with pytest.raises(RuntimeError, match="changed after preview"):
        apply_plan(path, plan)


@pytest.mark.asyncio
async def test_manual_exchange_position_is_separate_and_does_not_fabricate_fill_or_pnl(tmp_path):
    path = tmp_path / "manual.db"
    await DatabaseManager(str(path)).initialize()
    with sqlite3.connect(path) as db:
        db.execute("""
            INSERT INTO reconciliation_alerts
            (id, severity, kind, market_id, expected_value, observed_value,
             first_seen_at, last_seen_at)
            VALUES (25, 'critical', 'position_mismatch_yes', 'ATP', '0', '1.67', 'now', 'now')
        """)
        db.commit()
    order = SimpleNamespace(market_id="ATP", order_id="manual-order", client_order_id="", status="executed")
    fill = SimpleNamespace(market_id="ATP", order_id="manual-order", fill_id="manual-fill")
    plan = build_account_activity_repairs(
        path, {("ATP", "YES"): 1.67}, [order], [fill], {"ATP": {"status": "active"}},
        manual_positions=[("ATP", "YES")],
    )
    assert plan["manual_review"] == []
    assert plan["repairs"][0]["invented_bot_fill"] is False
    assert plan["repairs"][0]["invented_pnl"] is False
    assert apply_account_activity_repairs(path, plan) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT market_id, side, quantity, source FROM external_account_positions").fetchone() == (
            "ATP", "YES", 1.67, "manual_exchange_activity"
        )
        assert db.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM trade_logs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_terminal_unverified_order_requires_complete_history_absence(tmp_path):
    path = tmp_path / "order.db"
    await DatabaseManager(str(path)).initialize()
    with sqlite3.connect(path) as db:
        db.execute("""
            INSERT INTO orders
            (id, client_order_id, submission_fingerprint, environment, strategy, market_id, side, action,
             order_type, requested_quantity, remaining_quantity, state, created_at)
            VALUES (2, 'uncertain-client', 'fingerprint-1', 'production', 'test', 'FINAL', 'YES', 'sell',
                    'limit', 30, 30, 'verification_failed', 'now')
        """)
        db.execute("""
            INSERT INTO reconciliation_alerts
            (id, severity, kind, order_id, market_id, first_seen_at, last_seen_at)
            VALUES (18, 'critical', 'live_order_verification_failed', 2, 'FINAL', 'now', 'now')
        """)
        db.commit()
    plan = build_account_activity_repairs(
        path, {}, [], [], {"FINAL": {"status": "finalized", "result": "no"}},
        terminal_order_alert_ids=[18],
    )
    assert plan["manual_review"] == []
    assert plan["repairs"][0]["record_after"]["state"] == "expired"
    assert apply_account_activity_repairs(path, plan) == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state, filled_quantity FROM orders WHERE id=2").fetchone() == ("expired", 0.0)
        assert db.execute("SELECT COUNT(*) FROM order_fills").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_terminal_unverified_order_blocks_when_exchange_match_exists(tmp_path):
    path = tmp_path / "order-match.db"
    await DatabaseManager(str(path)).initialize()
    with sqlite3.connect(path) as db:
        db.execute("""
            INSERT INTO orders
            (id, client_order_id, submission_fingerprint, environment, strategy, market_id, side, action,
             order_type, requested_quantity, remaining_quantity, state, created_at)
            VALUES (2, 'uncertain-client', 'fingerprint-2', 'production', 'test', 'FINAL', 'YES', 'sell',
                    'limit', 30, 30, 'verification_failed', 'now')
        """)
        db.execute("""
            INSERT INTO reconciliation_alerts
            (id, severity, kind, order_id, market_id, first_seen_at, last_seen_at)
            VALUES (18, 'critical', 'live_order_verification_failed', 2, 'FINAL', 'now', 'now')
        """)
        db.commit()
    remote = SimpleNamespace(market_id="FINAL", order_id="remote", client_order_id="uncertain-client", status="executed")
    plan = build_account_activity_repairs(
        path, {}, [remote], [], {"FINAL": {"status": "finalized", "result": "no"}},
        terminal_order_alert_ids=[18],
    )
    assert plan["repairs"] == []
    assert "authoritative order or fill exists" in " ".join(plan["manual_review"][0]["manual_review_reasons"])


def test_only_exact_alert_ids_are_resolved(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}}
    )
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO reconciliation_alerts VALUES "
            "(2,'critical','unrelated_critical','TEST-A',NULL)"
        )
        db.commit()
    assert apply_plan(path, plan) == 1
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT id, resolved_at IS NOT NULL FROM reconciliation_alerts ORDER BY id"
        ).fetchall()
    assert rows == [(1, 1), (2, 0)]


def test_no_fill_exit_price_or_pnl_is_invented(tmp_path):
    path = tmp_path / "ledger.db"
    database(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE positions ADD COLUMN average_exit_price REAL")
        db.execute("ALTER TABLE positions ADD COLUMN realized_pnl REAL")
        db.commit()
    plan = build_plan(
        path, local_candidates(path, {}), {"TEST-A": {"status": "settled"}}
    )
    apply_plan(path, plan)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT average_exit_price, realized_pnl FROM positions WHERE id=7"
        ).fetchone() == (None, None)
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_snapshot_change_between_preview_and_apply_aborts():
    preview = {
        "plan_id": "same",
        "exchange_snapshot": {"positions": [], "active_orders": [], "markets": {}},
    }
    changed = {
        "plan_id": "different",
        "exchange_snapshot": {
            "positions": [{"market_id": "A", "side": "YES", "quantity": 1}],
            "active_orders": [], "markets": {},
        },
    }
    with pytest.raises(ReadOnlyValidationError, match="snapshot changed"):
        assert_plan_unchanged(preview, changed)


@pytest.mark.asyncio
async def test_apply_persists_post_repair_reconciliation_on_source(tmp_path, monkeypatch):
    source = tmp_path / "persistent.db"
    await DatabaseManager(str(source)).initialize()
    with sqlite3.connect(source) as db:
        cursor = db.execute("""
            INSERT INTO positions
            (market_id, side, entry_price, quantity, timestamp, live, status,
             open_quantity, reconciliation_status)
            VALUES ('SETTLED-A', 'YES', .4, 2, '2026-01-01', 1, 'open', 2, NULL)
        """)
        position_id = cursor.lastrowid
        db.execute("""
            INSERT INTO reconciliation_alerts
            (severity, kind, market_id, expected_value, observed_value,
             first_seen_at, last_seen_at)
            VALUES ('critical', 'position_mismatch_yes', 'SETTLED-A', '2', '0', 'now', 'now')
        """)
        db.commit()

    class ReadClient:
        async def get_positions(self):
            return {"market_positions": []}

        async def get_all_orders(self, **kwargs):
            return []

        async def get_all_fills(self, **kwargs):
            return []

        async def get_market(self, ticker):
            assert ticker == "SETTLED-A"
            return {"market": {"status": "settled", "result": "yes"}}

    class RawClient:
        async def close(self):
            return None

    async def fake_read_only_client(environment):
        return ReadClient(), RawClient()

    monkeypatch.setattr(repair_stale_positions, "read_only_client", fake_read_only_client)
    preview_args = SimpleNamespace(
        db=source, backup_dir=tmp_path / "backups", apply=False, confirm="",
    )
    preview = await repair_stale_positions.run(preview_args, {})
    assert preview["repair_count"] == 1
    apply_args = SimpleNamespace(
        db=source, backup_dir=tmp_path / "backups",
        apply=True, confirm=preview["plan_id"],
    )
    applied = await repair_stale_positions.run(apply_args, {})
    assert applied["canary_ready"] is True
    assert applied["post_repair_checkpoint_fresh"] is True
    with sqlite3.connect(source) as db:
        assert db.execute(
            "SELECT status FROM positions WHERE id=?", (position_id,)
        ).fetchone()[0] == "administratively_reconciled"
        run = db.execute(
            "SELECT trigger, status FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run[0] == "post_local_repair_apply"
    assert run[1] in {"completed", "completed_with_mismatches"}


def test_successful_preview_report_always_contains_required_keys():
    report = complete_report({"mode": "PREVIEW_ONLY", "plan_id": "plan"})
    assert set(REQUIRED_REPORT_KEYS).issubset(report)
    assert report["remaining_critical_alerts"] is None
    assert report["remaining_warning_alerts"] is None


def test_successful_preview_stdout_is_valid_json_when_runtime_logs_exist(
    monkeypatch, capsys, tmp_path,
):
    async def fake_run(args, environment, progress=None):
        print("runtime log line")
        return {"mode": "PREVIEW_ONLY", "plan_id": "plan"}

    database_path = tmp_path / "ledger.db"
    database_path.touch()
    monkeypatch.setattr(repair_stale_positions, "run", fake_run)
    monkeypatch.setattr(
        repair_stale_positions.sys,
        "argv",
        ["repair_stale_positions.py", "--db", str(database_path)],
    )
    assert repair_stale_positions.main() == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert set(REQUIRED_REPORT_KEYS).issubset(report)
    assert "runtime log line" not in captured.out
    assert "runtime log line" in captured.err


def test_redirected_subprocess_stdout_is_exactly_one_json_object(tmp_path):
    database_path = tmp_path / "ledger.db"
    with sqlite3.connect(database_path):
        pass
    preview_path = tmp_path / "preview.json"
    environment = dict(os.environ)
    for name in (
        "KALSHI_API_KEY", "KALSHI_PRIVATE_KEY_PATH", "READ_ONLY_ACCOUNT_VALIDATION",
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED", "LIVE_ORDER_SUBMISSION_KILL_SWITCH",
        "RECONCILIATION_SHADOW_MODE", "LIVE_TRADING_ENABLED",
    ):
        environment.pop(name, None)
    # load_dotenv() does not override this explicit fail-closed value, so the
    # subprocess can never construct a client even if a developer has a .env.
    environment["AUTHORITATIVE_LIVE_EXECUTION_ENABLED"] = "true"
    with preview_path.open("w", encoding="utf-8") as output:
        completed = subprocess.run(
            [sys.executable, "scripts/repair_stale_positions.py", "--db", str(database_path)],
            cwd=repair_stale_positions.ROOT, env=environment, stdout=output,
            stderr=subprocess.PIPE, text=True, timeout=30, check=False,
        )
    assert completed.returncode == 2
    with preview_path.open("r", encoding="utf-8") as source:
        report = json.load(source)
    assert set(REQUIRED_REPORT_KEYS).issubset(report)
    assert report["mode"] == "BLOCKED"
    assert "Traceback (most recent call last)" in report["error_traceback"]


def test_backup_waits_for_transient_sqlite_lock(tmp_path):
    source = tmp_path / "locked.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE proof (value TEXT)")
        db.execute("INSERT INTO proof VALUES ('preserved')")
        db.commit()
    locker = sqlite3.connect(source, timeout=1, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")

    def release():
        time.sleep(.2)
        locker.rollback()
        locker.close()

    thread = threading.Thread(target=release)
    thread.start()
    copied = backup(source, tmp_path / "backups")
    thread.join(timeout=2)
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT value FROM proof").fetchone()[0] == "preserved"

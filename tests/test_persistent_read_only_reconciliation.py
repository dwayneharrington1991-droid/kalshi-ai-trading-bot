import sqlite3
from datetime import datetime, timezone

import pytest

from scripts.read_only_validate import ENVIRONMENT_URLS, ReadOnlyAccountClient
from scripts.run_persistent_read_only_reconciliation import (
    PersistentReconciliationError,
    run_persistent_reconciliation,
    validate_persistent_safety,
)
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


def safe_environment(tmp_path, **overrides):
    key = tmp_path / "production.pem"
    key.write_text("mock-key", encoding="utf-8")
    values = {
        "KALSHI_ENVIRONMENT": "production",
        "KALSHI_API_KEY": "TEST_ONLY_KEY",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "READ_ONLY_ACCOUNT_VALIDATION": "true",
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "false",
        "LIVE_ORDER_SUBMISSION_KILL_SWITCH": "true",
        "LIVE_TRADING_ENABLED": "false",
        "RECONCILIATION_SHADOW_MODE": "true",
    }
    values.update(overrides)
    return values


class FakeClient:
    environment = "production"
    base_url = ENVIRONMENT_URLS["production"]

    def __init__(self, fail_orders=False):
        self.closed = False
        self.fail_orders = fail_orders
        self.order_calls = 0

    async def get_balance(self):
        return {"balance": 500}

    async def get_positions(self):
        return {"market_positions": []}

    async def get_all_orders(self, **kwargs):
        self.order_calls += 1
        # The first call is the explicit resting-order preflight. Fail only
        # when reconciliation performs its independent authoritative read.
        if self.fail_orders and self.order_calls > 1:
            raise RuntimeError("secret remote failure")
        return []

    async def get_all_fills(self, **kwargs):
        return []

    async def get_markets(self, **kwargs):
        return {"markets": [], "cursor": None}

    async def close(self):
        self.closed = True

    async def place_order(self, **kwargs):
        raise AssertionError("write reached")

    async def cancel_order(self, **kwargs):
        raise AssertionError("write reached")


@pytest.mark.parametrize("overrides", [
    {"KALSHI_ENVIRONMENT": "demo"},
    {"READ_ONLY_ACCOUNT_VALIDATION": "false"},
    {"AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "true"},
    {"LIVE_ORDER_SUBMISSION_KILL_SWITCH": "false"},
    {"LIVE_TRADING_ENABLED": "true"},
    {"RECONCILIATION_SHADOW_MODE": "false"},
])
async def test_missing_or_unsafe_gate_blocks_before_client(tmp_path, overrides):
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        return FakeClient()

    with pytest.raises(Exception):
        await run_persistent_reconciliation(
            environment=safe_environment(tmp_path, **overrides),
            client_factory=factory, db_path=str(tmp_path / "missing.db"),
        )
    assert called is False


async def test_facade_has_no_exchange_write_methods():
    facade = ReadOnlyAccountClient(FakeClient(), "production")
    for name in (
        "place_order", "cancel_order", "amend_order", "replace_order",
        "batch_create_orders", "batch_cancel_orders", "withdraw", "transfer",
    ):
        assert not hasattr(facade, name)


async def test_persistent_database_receives_completed_checkpoint_and_backup(tmp_path):
    database = tmp_path / "persistent.db"
    await DatabaseManager(str(database)).initialize()
    client = FakeClient()
    result = await run_persistent_reconciliation(
        environment=safe_environment(tmp_path),
        client_factory=lambda environment: client,
        db_path=str(database), backup_dir=tmp_path / "backups",
    )
    assert result["persistent_reconciliation"] == "COMPLETE"
    assert result["reconciliation_status"] == "completed"
    assert result["database_path"] == str(database.resolve())
    assert result["checkpoint_age_seconds"] <= 30
    assert client.closed is True
    assert list((tmp_path / "backups").glob("*.db"))
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT status, checkpoint, summary FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row[0] == "completed"
    assert row[1]
    assert '"exchange_writes": false' in row[2]


async def test_failure_never_creates_healthy_checkpoint(tmp_path):
    database = tmp_path / "persistent.db"
    await DatabaseManager(str(database)).initialize()
    client = FakeClient(fail_orders=True)
    with pytest.raises(Exception):
        await run_persistent_reconciliation(
            environment=safe_environment(tmp_path),
            client_factory=lambda environment: client,
            db_path=str(database), backup_dir=tmp_path / "backups",
        )
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT status, checkpoint FROM reconciliation_runs"
        ).fetchall()
    assert rows and rows[-1][0] == "failed"
    assert not any(status in {"completed", "completed_with_mismatches"} for status, _ in rows)
    assert not any(checkpoint for _, checkpoint in rows)
    assert client.closed is True


async def test_existing_critical_alert_is_not_silently_resolved(tmp_path):
    database = tmp_path / "persistent.db"
    await DatabaseManager(str(database)).initialize()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as db:
        db.execute("""
            INSERT INTO reconciliation_alerts
            (severity, kind, details, first_seen_at, last_seen_at)
            VALUES ('critical', 'manual_review_required', 'test', ?, ?)
        """, (now, now))
        db.commit()
    result = await run_persistent_reconciliation(
        environment=safe_environment(tmp_path),
        client_factory=lambda environment: FakeClient(),
        db_path=str(database), backup_dir=tmp_path / "backups",
    )
    assert result["critical_alert_count"] == 1
    with sqlite3.connect(database) as db:
        unresolved = db.execute(
            "SELECT COUNT(*) FROM reconciliation_alerts WHERE resolved_at IS NULL "
            "AND kind='manual_review_required'"
        ).fetchone()[0]
    assert unresolved == 1

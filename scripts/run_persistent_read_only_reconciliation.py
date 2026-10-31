#!/usr/bin/env python3
"""Create one persistent, GET-only production reconciliation checkpoint."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.read_only_validate import (  # noqa: E402
    ENVIRONMENT_URLS,
    ReadOnlyAccountClient,
    ReadOnlyValidationError,
    _close_client,
    _position_count,
    validate_read_only_safety,
)


class PersistentReconciliationError(RuntimeError):
    """A sanitized, fail-closed persistent reconciliation failure."""


WRITE_CAPABILITIES = (
    "place_order", "cancel_order", "amend_order", "replace_order",
    "batch_create_orders", "batch_cancel_orders", "withdraw", "withdrawal", "transfer",
)


def validate_persistent_safety(environment: Mapping[str, str]) -> str:
    """Require exact production read-only gates before constructing a client."""
    selected = validate_read_only_safety(environment)
    if selected != "production":
        raise PersistentReconciliationError("persistent reconciliation requires production")
    if environment.get("LIVE_TRADING_ENABLED", "").lower() != "false":
        raise PersistentReconciliationError("LIVE_TRADING_ENABLED must be explicitly false")
    if environment.get("RECONCILIATION_SHADOW_MODE", "").lower() != "true":
        raise PersistentReconciliationError("reconciliation shadow mode must remain enabled")
    return selected


def _assert_read_only_facade(client: ReadOnlyAccountClient) -> None:
    for name in WRITE_CAPABILITIES:
        if hasattr(client, name):
            raise PersistentReconciliationError("write-capable account client is forbidden")
    if hasattr(client, "_ReadOnlyAccountClient__client") or hasattr(client, "__dict__"):
        raise PersistentReconciliationError("unrestricted account client is exposed")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def backup_database(source: Path, backup_dir: Optional[Path] = None) -> Path:
    """Take a consistent SQLite backup without writing to the source ledger."""
    if not source.is_file():
        raise PersistentReconciliationError("persistent database does not exist")
    destination_dir = backup_dir or source.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / f"{source.stem}.pre-reconciliation-{_timestamp()}.db"
    try:
        with sqlite3.connect(
            f"file:{source.resolve().as_posix()}?mode=ro", uri=True, timeout=30
        ) as src, sqlite3.connect(target, timeout=30) as dst:
            src.execute("PRAGMA busy_timeout = 30000")
            dst.execute("PRAGMA busy_timeout = 30000")
            src.backup(dst, pages=256, sleep=0.05)
    except sqlite3.Error:
        target.unlink(missing_ok=True)
        raise PersistentReconciliationError("persistent database backup failed") from None
    return target


def _alert_counts(path: Path) -> tuple[int, int]:
    try:
        with sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True) as db:
            rows = dict(db.execute("""
                SELECT severity, COUNT(*) FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity IN ('critical', 'warning')
                GROUP BY severity
            """).fetchall())
        return int(rows.get("critical", 0)), int(rows.get("warning", 0))
    except sqlite3.Error:
        raise PersistentReconciliationError("reconciliation alerts cannot be verified") from None


async def run_persistent_reconciliation(
    *,
    environment: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
    db_path: Optional[str] = None,
    backup_dir: Optional[Path] = None,
) -> dict:
    """Run one bounded reconciliation against the named persistent ledger."""
    environment = environment or os.environ
    selected = validate_persistent_safety(environment)
    source = Path(db_path or environment.get("DB_PATH", "trading_system.db")).resolve()
    saved_backup = backup_database(source, backup_dir)

    from src.clients.kalshi_client import KalshiClient
    from src.orders.reconciler import OrderReconciler
    from src.orders.repository import OrderRepository
    from src.utils.database import DatabaseManager

    # Migrations are transactional and run only after the source backup exists.
    await DatabaseManager(str(source)).initialize()

    raw_client = None
    try:
        if client_factory:
            raw_client = client_factory(environment=selected)
        else:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                raw_client = KalshiClient(environment=selected)
        if (
            getattr(raw_client, "environment", None) != selected
            or getattr(raw_client, "base_url", None) != ENVIRONMENT_URLS[selected]
        ):
            raise PersistentReconciliationError("Kalshi client environment or host mismatch")
        read_client = ReadOnlyAccountClient(raw_client, selected)
        _assert_read_only_facade(read_client)

        balance_response = await read_client.get_balance()
        balance = balance_response.get("balance") if isinstance(balance_response, dict) else None
        if not isinstance(balance, (int, float)) or isinstance(balance, bool):
            raise PersistentReconciliationError("account balance could not be verified")
        positions_response = await read_client.get_positions()
        resting_orders = await read_client.get_all_orders(status="resting", limit=1000)
        fills = await read_client.get_all_fills(limit=1000)
        markets_response = await read_client.get_markets(limit=1, status="open")
        if not isinstance(markets_response, dict) or not isinstance(
            markets_response.get("markets"), list
        ):
            raise PersistentReconciliationError("market data could not be verified")

        repository = OrderRepository(str(source))
        result = await OrderReconciler(
            repository, read_client, shadow_mode=True, paper_mode=False,
            project_positions=False,
        ).reconcile(trigger="persistent_read_only_bootstrap", full=True)
        if result.status not in {"completed", "completed_with_mismatches"} or result.run_id is None:
            raise PersistentReconciliationError("persistent reconciliation did not complete")

        # Attach bounded audit metadata to the successfully completed run.
        audit = json.dumps({
            "mode": "persistent_read_only",
            "environment": selected,
            "exchange_writes": False,
            "backup_created": True,
        }, sort_keys=True)
        try:
            with sqlite3.connect(source, timeout=30) as db:
                db.execute("PRAGMA busy_timeout = 30000")
                db.execute("BEGIN IMMEDIATE")
                updated = db.execute(
                    "UPDATE reconciliation_runs SET checkpoint = ?, summary = ? WHERE id = ? "
                    "AND status IN ('completed', 'completed_with_mismatches')",
                    (datetime.now(timezone.utc).isoformat(), audit, result.run_id),
                )
                if updated.rowcount != 1:
                    raise PersistentReconciliationError("reconciliation audit checkpoint was not written")
                db.commit()
        except sqlite3.Error:
            raise PersistentReconciliationError("reconciliation audit checkpoint was not written") from None

        health = await repository.get_reconciliation_health(30)
        critical, warning = _alert_counts(source)
        if not health["healthy_status"]:
            raise PersistentReconciliationError("healthy reconciliation checkpoint is unavailable")
        try:
            with sqlite3.connect(
                f"file:{source.resolve().as_posix()}?mode=ro", uri=True
            ) as db:
                checkpoint_timestamp = db.execute(
                    "SELECT completed_at FROM reconciliation_runs WHERE id = ?",
                    (result.run_id,),
                ).fetchone()[0]
        except (sqlite3.Error, TypeError):
            raise PersistentReconciliationError("checkpoint timestamp cannot be verified") from None
        return {
            "database_path": str(source),
            "backup_path": str(saved_backup),
            "environment": selected,
            "authentication": "SUCCESS",
            "exchange_position_count": _position_count(positions_response),
            "resting_order_count": len(resting_orders),
            "fill_count": len(fills),
            "reconciliation_status": result.status,
            "critical_alert_count": critical,
            "warning_alert_count": warning,
            "checkpoint_timestamp": checkpoint_timestamp,
            "checkpoint_age_seconds": health["age_seconds"],
            "persistent_reconciliation": "COMPLETE",
        }
    except (ReadOnlyValidationError, PersistentReconciliationError):
        raise
    except BaseException:
        raise PersistentReconciliationError("persistent reconciliation failed") from None
    finally:
        if raw_client is not None:
            await _close_client(raw_client)


def _print_summary(result: dict) -> None:
    fields = (
        ("DATABASE_PATH", "database_path"), ("BACKUP_PATH", "backup_path"),
        ("ENVIRONMENT", "environment"), ("AUTHENTICATION", "authentication"),
        ("EXCHANGE_POSITION_COUNT", "exchange_position_count"),
        ("RESTING_ORDER_COUNT", "resting_order_count"), ("FILL_COUNT", "fill_count"),
        ("RECONCILIATION_STATUS", "reconciliation_status"),
        ("CRITICAL_ALERT_COUNT", "critical_alert_count"),
        ("WARNING_ALERT_COUNT", "warning_alert_count"),
        ("CHECKPOINT_TIMESTAMP", "checkpoint_timestamp"),
        ("CHECKPOINT_AGE_SECONDS", "checkpoint_age_seconds"),
        ("PERSISTENT_RECONCILIATION", "persistent_reconciliation"),
    )
    for label, key in fields:
        print(f"{label}={result.get(key, 'UNAVAILABLE')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    try:
        # Application components use stdout/stderr logging. Suppress those
        # internals so the command emits only the fixed sanitized summary.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = asyncio.run(run_persistent_reconciliation(
                db_path=args.db, backup_dir=args.backup_dir
            ))
    except BaseException as exc:
        failure = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "FAILURE"
        print(f"AUTHENTICATION={failure}")
        print("PERSISTENT_RECONCILIATION=BLOCKED")
        return 2
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

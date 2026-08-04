#!/usr/bin/env python3
"""Preview or explicitly apply local-only repairs for exchange-zero positions."""

import argparse
import asyncio
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Some application modules configure logging while they are imported. Keep
# those diagnostics on stderr so shell redirection of stdout always captures
# exactly one JSON document.
with contextlib.redirect_stdout(sys.stderr):
    from scripts.read_only_validate import (  # noqa: E402
        ENVIRONMENT_URLS, ReadOnlyAccountClient, ReadOnlyValidationError,
        _close_client, validate_read_only_safety,
    )
    from src.clients.kalshi_client import KalshiClient  # noqa: E402
    from src.orders.local_position_repair import (  # noqa: E402
        apply_plan, build_plan, local_candidates, remote_position_map,
        unresolved_critical_count,
    )
    from src.orders.reconciler import OrderReconciler  # noqa: E402
    from src.orders.repository import OrderRepository  # noqa: E402
    from src.utils.database import DatabaseManager  # noqa: E402


REQUIRED_REPORT_KEYS = (
    "mode",
    "plan_id",
    "source_ledger_modified",
    "simulated_or_applied_count",
    "pre_repair_critical_alert_count",
    "post_repair_critical_alert_count",
    "remaining_critical_alerts",
    "remaining_warning_alerts",
    "post_repair_reconciliation_status",
    "canary_ready",
)


def complete_report(report: dict) -> dict:
    """Guarantee a stable successful-report schema without inventing unavailable values."""
    completed = dict(report)
    for key in REQUIRED_REPORT_KEYS:
        completed.setdefault(key, None)
    return completed


def backup(source: Path, directory: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{source.stem}.pre-local-repair-{timestamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for attempt in range(3):
        try:
            with sqlite3.connect(
                f"file:{source.resolve().as_posix()}?mode=ro", uri=True, timeout=30
            ) as src:
                src.execute("PRAGMA busy_timeout = 30000")
                with sqlite3.connect(target, timeout=30) as dst:
                    dst.execute("PRAGMA busy_timeout = 30000")
                    src.backup(dst, pages=256, sleep=0.05)
            return target
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            target.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
    raise last_error


async def read_only_client(environment: dict[str, str]):
    selected = validate_read_only_safety(environment)
    if environment.get("LIVE_TRADING_ENABLED", "").lower() != "false":
        raise ReadOnlyValidationError("LIVE_TRADING_ENABLED must be explicitly false")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        client = KalshiClient(environment=selected)
    if client.base_url != ENVIRONMENT_URLS[selected]:
        await _close_client(client)
        raise ReadOnlyValidationError("Kalshi client host mismatch")
    read_client = ReadOnlyAccountClient(client, selected)
    return read_client, client


async def authoritative_plan(db_path: Path, read_client: ReadOnlyAccountClient):
    positions = await read_client.get_positions()
    candidates = local_candidates(db_path, remote_position_map(positions))
    markets = {}
    for ticker in sorted({row["market_id"] for row in candidates}):
        markets[ticker] = await read_client.get_market(ticker)
    return build_plan(db_path, candidates, markets)


def alert_snapshot(db_path: Path, severity: str):
    """Return bounded, secret-free unresolved alert details or None if unavailable."""
    try:
        with sqlite3.connect(db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""
                SELECT id, kind, market_id, expected_value, observed_value
                FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity = ?
                ORDER BY kind, market_id, id
            """, (severity,)).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return None


async def run(args, environment: dict[str, str], progress: dict | None = None) -> dict:
    progress = progress if progress is not None else {}
    progress["stage"] = "resolve_source_ledger"
    source = args.db.resolve(strict=True)
    progress["stage"] = "backup_source_ledger"
    saved_backup = backup(source, args.backup_dir or source.parent / "backups")
    tempdir = Path(tempfile.mkdtemp(prefix="kalshi-local-repair-preview-"))
    working_db = tempdir / "ledger.db"
    shutil.copy2(saved_backup, working_db)
    progress["stage"] = "construct_read_only_client"
    read_client, raw_client = await read_only_client(environment)
    try:
        # The source ledger may predate reconciliation migrations. Initialize
        # and populate alerts only on the disposable copy, never on source.
        progress["stage"] = "initialize_disposable_ledger"
        await DatabaseManager(str(working_db)).initialize()
        progress["stage"] = "pre_repair_reconciliation"
        before = await OrderReconciler(
            OrderRepository(str(working_db)), read_client,
            shadow_mode=True, paper_mode=False, project_positions=False,
        ).reconcile(trigger="pre_local_repair_preview", full=True)
        pre_critical_alerts = alert_snapshot(working_db, "critical")
        progress["stage"] = "build_authoritative_plan"
        plan = await authoritative_plan(working_db, read_client)
        progress["plan_id"] = plan["plan_id"]
        if args.apply:
            if args.confirm != plan["plan_id"]:
                raise ReadOnlyValidationError("confirmation must exactly match current plan_id")
            applied = apply_plan(source, plan)
            verification_db = tempdir / "verification.db"
            shutil.copy2(source, verification_db)
            await DatabaseManager(str(verification_db)).initialize()
            mode = "APPLIED_LOCAL_ONLY"
        else:
            verification_db = working_db
            progress["stage"] = "simulate_local_repair"
            applied = apply_plan(verification_db, plan)
            mode = "PREVIEW_ONLY"
        reconciler = OrderReconciler(
            OrderRepository(str(verification_db)), read_client,
            shadow_mode=True, paper_mode=False, project_positions=False,
        )
        progress["stage"] = "post_repair_reconciliation"
        result = await reconciler.reconcile(trigger="post_local_repair_preview", full=True)
        remaining_critical = alert_snapshot(verification_db, "critical")
        remaining_warning = alert_snapshot(verification_db, "warning")
        post_critical_count = (
            len(remaining_critical) if remaining_critical is not None
            else unresolved_critical_count(verification_db)
        )
        report = {
            "mode": mode,
            "exchange_writes": False,
            "source_ledger_modified": bool(args.apply),
            "backup_path": str(saved_backup),
            "plan_id": plan["plan_id"],
            "repairs": plan["repairs"],
            "repair_count": len(plan["repairs"]),
            "simulated_or_applied_count": applied,
            "pre_repair_reconciliation_status": before.status,
            "pre_repair_mismatch_count": before.mismatch_count,
            "pre_repair_critical_alert_count": (
                len(pre_critical_alerts) if pre_critical_alerts is not None else None
            ),
            "post_repair_reconciliation_status": result.status,
            "post_repair_mismatch_count": result.mismatch_count,
            "post_repair_critical_alert_count": post_critical_count,
            "post_repair_unresolved_critical_alerts": post_critical_count,
            "remaining_critical_alerts": remaining_critical,
            "remaining_warning_alerts": remaining_warning,
            "canary_ready": bool(
                post_critical_count == 0
                and result.status in {"completed", "completed_with_mismatches"}
                and applied == len(plan["repairs"])
            ),
            "apply_command_requires_exact_plan_id": not args.apply,
        }
        return complete_report(report)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)
        await _close_client(raw_client)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.confirm and not args.apply:
        parser.error("--confirm is valid only with --apply")
    load_dotenv(ROOT / ".env")
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    progress = {}
    try:
        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            report = complete_report(asyncio.run(run(args, dict(os.environ), progress)))
    except Exception as exc:
        error_traceback = traceback.format_exc()
        report = complete_report({
            "mode": "BLOCKED",
            "plan_id": progress.get("plan_id"),
            "source_ledger_modified": False,
            "canary_ready": False,
            "error": type(exc).__name__,
            "error_stage": progress.get("stage"),
            "error_message": str(exc),
            "error_traceback": error_traceback,
        })
        for captured in (captured_stdout.getvalue(), captured_stderr.getvalue()):
            if captured:
                print(captured, file=sys.stderr, end="" if captured.endswith("\n") else "\n")
        print(error_traceback, file=sys.stderr, end="" if error_traceback.endswith("\n") else "\n")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    for captured in (captured_stdout.getvalue(), captured_stderr.getvalue()):
        if captured:
            print(captured, file=sys.stderr, end="" if captured.endswith("\n") else "\n")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

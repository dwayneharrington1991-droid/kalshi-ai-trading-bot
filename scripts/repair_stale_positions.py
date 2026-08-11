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
        active_order_snapshot, apply_plan, build_plan, local_candidates, remote_position_map,
        unresolved_critical_count, build_account_activity_repairs,
        apply_account_activity_repairs,
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


async def authoritative_plan(
    db_path: Path, read_client: ReadOnlyAccountClient,
    preview_paper_simulation_alert_ids=(),
    manual_positions=(), terminal_order_alert_ids=(),
):
    positions_response = await read_client.get_positions()
    positions = remote_position_map(positions_response)
    all_orders = await read_client.get_all_orders(limit=1000)
    all_fills = await read_client.get_all_fills(limit=1000)
    orders = active_order_snapshot(all_orders)
    candidates = local_candidates(db_path, positions)
    markets = {}
    terminal_markets = []
    with sqlite3.connect(db_path) as db:
        for alert_id in terminal_order_alert_ids:
            row = db.execute("SELECT market_id FROM reconciliation_alerts WHERE id = ?", (alert_id,)).fetchone()
            if row and row[0]:
                terminal_markets.append(row[0])
    for ticker in sorted({row["market_id"] for row in candidates} | {row[0] for row in manual_positions} | set(terminal_markets)):
        markets[ticker] = await read_client.get_market(ticker)
    position_plan = build_plan(
        db_path, candidates, markets, active_orders=orders, remote_positions=positions,
        exchange_orders=all_orders, exchange_fills=all_fills,
        preview_paper_simulation_alert_ids=preview_paper_simulation_alert_ids,
        log_dir=ROOT / "logs",
    )
    account_plan = build_account_activity_repairs(
        db_path, positions, all_orders, all_fills, markets,
        manual_positions=manual_positions, terminal_order_alert_ids=terminal_order_alert_ids,
    )
    combined = {
        "repairs": position_plan["repairs"],
        "account_activity_repairs": account_plan["repairs"],
        "manual_review": position_plan["manual_review"] + account_plan["manual_review"],
        "exchange_snapshot": position_plan["exchange_snapshot"],
    }
    canonical = json.dumps(combined, sort_keys=True, separators=(",", ":"))
    import hashlib
    combined["plan_id"] = hashlib.sha256(canonical.encode()).hexdigest()
    return combined


def position_mismatch_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as db:
        return int(db.execute("""
            SELECT COUNT(*) FROM reconciliation_alerts
            WHERE resolved_at IS NULL AND severity = 'critical'
              AND kind IN ('position_mismatch_yes', 'position_mismatch_no')
        """).fetchone()[0])


def unresolved_critical_ids(db_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as db:
        return [int(row[0]) for row in db.execute("""
            SELECT id FROM reconciliation_alerts
            WHERE resolved_at IS NULL AND severity = 'critical' ORDER BY id
        """).fetchall()]


def assert_plan_unchanged(preview_plan: dict, current_plan: dict) -> None:
    if current_plan["exchange_snapshot"] != preview_plan["exchange_snapshot"]:
        raise ReadOnlyValidationError("authoritative exchange snapshot changed after preview")
    if current_plan["plan_id"] != preview_plan["plan_id"]:
        raise ReadOnlyValidationError("local or exchange assumptions changed after preview")


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
        # Plan from the untouched source. Reconciliation above is diagnostic
        # and intentionally writes only to the disposable working copy.
        preview_paper_ids = tuple(
            getattr(args, "preview_paper_simulation_alert", ()) or ()
        )
        apply_paper_ids = tuple(
            getattr(args, "allow_paper_simulation_alert", ()) or ()
        )
        paper_alert_ids = apply_paper_ids if args.apply else preview_paper_ids
        manual_positions = tuple(
            (value.rsplit(":", 1)[0], value.rsplit(":", 1)[1].upper())
            for value in (getattr(args, "manual_exchange_position", ()) or ())
        )
        terminal_order_alert_ids = tuple(
            getattr(args, "terminal_unverified_order_alert", ()) or ()
        )
        plan = await authoritative_plan(
            source, read_client, paper_alert_ids, manual_positions, terminal_order_alert_ids
        )
        progress["plan_id"] = plan["plan_id"]
        if args.apply:
            if args.confirm != plan["plan_id"]:
                raise ReadOnlyValidationError("confirmation must exactly match current plan_id")
            planned_alert_ids = sorted({
                alert_id for repair in plan["repairs"] for alert_id in repair["alert_ids"]
            })
            planned_alert_ids.extend(
                repair["alert_id"] for repair in plan["account_activity_repairs"]
            )
            planned_alert_ids = sorted(planned_alert_ids)
            if plan["manual_review"] or unresolved_critical_ids(source) != planned_alert_ids:
                raise ReadOnlyValidationError(
                    "all unresolved critical alerts must be safely repairable in one plan"
                )
            progress["stage"] = "second_authoritative_snapshot"
            second_plan = await authoritative_plan(
                source, read_client, paper_alert_ids, manual_positions, terminal_order_alert_ids
            )
            assert_plan_unchanged(plan, second_plan)
            progress["stage"] = "apply_exact_plan"
            applied = apply_plan(source, plan)
            account_plan = {"plan_id": plan["plan_id"], "repairs": plan["account_activity_repairs"]}
            applied += apply_account_activity_repairs(source, account_plan)
            verification_db = source
            mode = "APPLIED_LOCAL_ONLY"
        else:
            verification_db = tempdir / "verification.db"
            shutil.copy2(saved_backup, verification_db)
            await DatabaseManager(str(verification_db)).initialize()
            progress["stage"] = "simulate_local_repair"
            applied = apply_plan(verification_db, plan)
            account_plan = {"plan_id": plan["plan_id"], "repairs": plan["account_activity_repairs"]}
            applied += apply_account_activity_repairs(verification_db, account_plan)
            mode = "PREVIEW_ONLY"
        reconciler = OrderReconciler(
            OrderRepository(str(verification_db)), read_client,
            shadow_mode=True, paper_mode=False, project_positions=False,
        )
        progress["stage"] = "post_repair_reconciliation"
        result = await reconciler.reconcile(
            trigger="post_local_repair_apply" if args.apply else "post_local_repair_preview",
            full=True,
        )
        health = await OrderRepository(str(verification_db)).get_reconciliation_health(30)
        remaining_critical = alert_snapshot(verification_db, "critical")
        remaining_warning = alert_snapshot(verification_db, "warning")
        remaining_position_mismatches = position_mismatch_count(verification_db)
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
            "account_activity_repairs": plan["account_activity_repairs"],
            "repair_count": len(plan["repairs"]) + len(plan["account_activity_repairs"]),
            "manual_review": plan["manual_review"],
            "manual_review_count": len(plan["manual_review"]),
            "simulated_or_applied_count": applied,
            "pre_repair_reconciliation_status": before.status,
            "pre_repair_mismatch_count": before.mismatch_count,
            "pre_repair_critical_alert_count": (
                len(pre_critical_alerts) if pre_critical_alerts is not None else None
            ),
            "post_repair_reconciliation_status": result.status,
            "post_repair_checkpoint_fresh": health["fresh"],
            "post_repair_mismatch_count": result.mismatch_count,
            "post_repair_critical_alert_count": post_critical_count,
            "post_repair_unresolved_critical_alerts": post_critical_count,
            "post_repair_position_mismatch_count": remaining_position_mismatches,
            "remaining_critical_alerts": remaining_critical,
            "remaining_warning_alerts": remaining_warning,
            "canary_ready": bool(
                post_critical_count == 0
                and result.status in {"completed", "completed_with_mismatches"}
                and health["healthy_status"] and health["fresh"]
                and remaining_position_mismatches == 0
                and applied == len(plan["repairs"]) + len(plan["account_activity_repairs"])
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
    parser.add_argument(
        "--preview-paper-simulation-alert", action="append", type=int, default=[],
        help="preview-only exact alert ID backed by contemporaneous paper-simulation evidence",
    )
    parser.add_argument(
        "--manual-exchange-position", action="append", default=[], metavar="TICKER:SIDE",
        help="exact user-confirmed manual exchange position to acknowledge separately from bot fills",
    )
    parser.add_argument(
        "--terminal-unverified-order-alert", action="append", type=int, default=[],
        help="exact critical alert for an uncertain order on a terminal market with no exchange history",
    )
    parser.add_argument(
        "--allow-paper-simulation-alert", action="append", type=int, default=[],
        help=(
            "apply-only exact alert ID already proven by contemporaneous "
            "paper-simulation evidence; still requires --confirm and an "
            "unchanged second authoritative snapshot"
        ),
    )
    args = parser.parse_args()
    if args.confirm and not args.apply:
        parser.error("--confirm is valid only with --apply")
    if args.apply and args.preview_paper_simulation_alert:
        parser.error("paper-simulation exceptions are preview-only")
    if args.allow_paper_simulation_alert and not args.apply:
        parser.error("--allow-paper-simulation-alert requires --apply")
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

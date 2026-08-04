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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def backup(source: Path, directory: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{source.stem}.pre-local-repair-{timestamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)
    return target


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


async def run(args, environment: dict[str, str]) -> dict:
    source = args.db.resolve(strict=True)
    saved_backup = backup(source, args.backup_dir or source.parent / "backups")
    tempdir = Path(tempfile.mkdtemp(prefix="kalshi-local-repair-preview-"))
    working_db = tempdir / "ledger.db"
    shutil.copy2(saved_backup, working_db)
    read_client, raw_client = await read_only_client(environment)
    try:
        # The source ledger may predate reconciliation migrations. Initialize
        # and populate alerts only on the disposable copy, never on source.
        await DatabaseManager(str(working_db)).initialize()
        before = await OrderReconciler(
            OrderRepository(str(working_db)), read_client,
            shadow_mode=True, paper_mode=False, project_positions=False,
        ).reconcile(trigger="pre_local_repair_preview", full=True)
        plan = await authoritative_plan(working_db, read_client)
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
            applied = apply_plan(verification_db, plan)
            mode = "PREVIEW_ONLY"
        reconciler = OrderReconciler(
            OrderRepository(str(verification_db)), read_client,
            shadow_mode=True, paper_mode=False, project_positions=False,
        )
        result = await reconciler.reconcile(trigger="post_local_repair_preview", full=True)
        critical = unresolved_critical_count(verification_db)
        return {
            "mode": mode,
            "exchange_writes": False,
            "backup_path": str(saved_backup),
            "plan_id": plan["plan_id"],
            "repairs": plan["repairs"],
            "repair_count": len(plan["repairs"]),
            "simulated_or_applied_count": applied,
            "pre_repair_reconciliation_status": before.status,
            "pre_repair_mismatch_count": before.mismatch_count,
            "post_repair_reconciliation_status": result.status,
            "post_repair_mismatch_count": result.mismatch_count,
            "post_repair_unresolved_critical_alerts": critical,
            "apply_command_requires_exact_plan_id": not args.apply,
        }
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
    try:
        report = asyncio.run(run(args, dict(os.environ)))
    except Exception as exc:
        print(f"LOCAL_RECONCILIATION_REPAIR=BLOCKED ({type(exc).__name__})")
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    print("LOCAL_RECONCILIATION_REPAIR=COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

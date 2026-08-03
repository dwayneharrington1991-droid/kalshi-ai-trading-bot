#!/usr/bin/env python3
"""Preview GET-only reconciliation against a disposable persistent-ledger copy."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.read_only_validate import (  # noqa: E402
    ReadOnlyValidationError,
    run_read_only_validation,
    validate_read_only_safety,
)

TRACKED_TABLES = (
    "positions",
    "orders",
    "order_fills",
    "order_state_events",
    "position_projection_baselines",
    "position_fill_projections",
    "reconciliation_alerts",
)
SENSITIVE_COLUMNS = {
    "raw_response", "raw_payload", "raw_submit_response", "raw_order_response",
    "verification_error", "details",
}


def _require_preview_safety(environment: dict[str, str]) -> None:
    validate_read_only_safety(environment)
    if environment.get("LIVE_TRADING_ENABLED", "").lower() != "false":
        raise ReadOnlyValidationError("LIVE_TRADING_ENABLED must be explicitly false")


def _consistent_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)


def _safe_value(column: str, value: Any) -> Any:
    if column in SENSITIVE_COLUMNS and value is not None:
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
        return f"<redacted sha256:{digest}>"
    return value


def _snapshot(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        existing = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for table in TRACKED_TABLES:
            if table not in existing:
                continue
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            primary = next((row[1] for row in db.execute(
                f"PRAGMA table_info({table})"
            ) if row[5]), columns[0])
            rows: dict[str, dict[str, Any]] = {}
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {primary}"):
                item = {column: _safe_value(column, row[column]) for column in columns}
                rows[str(row[primary])] = item
            result[table] = rows
    return result


def _diff(before: dict, after: dict) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for table in sorted(set(before) | set(after)):
        old_rows, new_rows = before.get(table, {}), after.get(table, {})
        for key in sorted(set(old_rows) | set(new_rows)):
            if key not in old_rows:
                changes.append({"operation": "insert", "table": table, "key": key,
                                "after": new_rows[key]})
            elif key not in new_rows:
                changes.append({"operation": "delete", "table": table, "key": key,
                                "before": old_rows[key]})
            elif old_rows[key] != new_rows[key]:
                fields = {
                    name: {"before": old_rows[key].get(name), "after": new_rows[key].get(name)}
                    for name in sorted(set(old_rows[key]) | set(new_rows[key]))
                    if old_rows[key].get(name) != new_rows[key].get(name)
                }
                changes.append({"operation": "update", "table": table, "key": key,
                                "fields": fields})
    return changes


def _mismatch_summary(snapshot: dict) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for alert in snapshot.get("reconciliation_alerts", {}).values():
        if alert.get("resolved_at") is not None:
            continue
        kind = alert.get("kind") or "unknown"
        if kind == "remote_only_order":
            assessment = "requires manual review before any local import"
        elif "position" in kind:
            assessment = "requires manual review; no exchange-side action proposed"
        elif kind in {"order_state_mismatch", "missing_fill", "fill_mismatch"}:
            assessment = "safely repairable locally after explicit approval"
        else:
            assessment = "requires manual review"
        item = summary.setdefault(kind, {"count": 0, "assessment": assessment})
        item["count"] += 1
    return summary


async def preview(db_path: Path, backup_dir: Path, environment: dict[str, str]) -> dict:
    _require_preview_safety(environment)
    source = db_path.resolve(strict=True)
    if not source.is_file():
        raise ReadOnlyValidationError("persistent ledger is not a regular file")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir.resolve() / f"{source.stem}.pre-reconciliation-{timestamp}.db"
    _consistent_backup(source, backup)
    before = _snapshot(backup)
    temporary_dir = Path(tempfile.mkdtemp(prefix="kalshi-reconciliation-preview-"))
    preview_db = temporary_dir / "ledger.db"
    try:
        shutil.copy2(backup, preview_db)
        result = await run_read_only_validation(environment=environment, db_path=str(preview_db))
        after = _snapshot(preview_db)
        changes = _diff(before, after)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
    counts: dict[str, int] = {}
    for change in changes:
        label = f"{change['table']}:{change['operation']}"
        counts[label] = counts.get(label, 0) + 1
    reconciliation = result["reconciliation"]
    return {
        "mode": "PREVIEW_ONLY",
        "exchange_writes": False,
        "source_ledger_modified": False,
        "backup_path": str(backup),
        "reconciliation_status": reconciliation.status,
        "mismatch_count": reconciliation.mismatch_count,
        "mismatch_categories": _mismatch_summary(after),
        "change_counts": counts,
        "proposed_local_changes": changes,
        "manual_approval_required_to_apply": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="Existing persistent SQLite ledger")
    parser.add_argument("--backup-dir", type=Path, help="Backup destination (default: <db-dir>/backups)")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    environment = dict(os.environ)
    backup_dir = args.backup_dir or args.db.parent / "backups"
    try:
        report = asyncio.run(preview(args.db, backup_dir, environment))
    except Exception as exc:
        print(f"PERSISTENT_RECONCILIATION_PREVIEW=BLOCKED ({type(exc).__name__})")
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    print("PERSISTENT_RECONCILIATION_PREVIEW=COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Local-only, exchange-authoritative repair planning for stale positions."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def remote_position_map(response: Any) -> dict[tuple[str, str], float]:
    if not isinstance(response, dict):
        return {}
    rows = response.get("market_positions", response.get("positions", []))
    result: dict[tuple[str, str], float] = {}
    for row in rows if isinstance(rows, list) else []:
        ticker = str(row.get("ticker") or row.get("market_ticker") or "")
        quantity = float(row.get("position_fp", row.get("position", 0)) or 0)
        if ticker and quantity:
            key = (ticker, "YES" if quantity > 0 else "NO")
            result[key] = result.get(key, 0.0) + abs(quantity)
    return result


def local_candidates(db_path: Path, remote: dict[tuple[str, str], float]) -> list[dict]:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT id, market_id, side, quantity, open_quantity, status, live,
                   timestamp, last_reconciled_at, reconciliation_status
            FROM positions
            WHERE live = 1 AND status IN ('open', 'pending')
            ORDER BY id
        """).fetchall()
        alert_keys = {
            (row["market_id"], str(row["kind"]).removeprefix("position_mismatch_").upper())
            for row in db.execute("""
                SELECT market_id, kind FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity = 'critical'
                  AND kind IN ('position_mismatch_yes', 'position_mismatch_no')
            """).fetchall()
        }
    totals: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (row["market_id"], str(row["side"]).upper())
        totals[key] = totals.get(key, 0.0) + float(row["quantity"])
    mismatch_keys = {key for key, quantity in totals.items()
                     if key in alert_keys and quantity and remote.get(key, 0.0) == 0.0}
    return [dict(row) for row in rows
            if (row["market_id"], str(row["side"]).upper()) in mismatch_keys]


def _market_payload(response: Any) -> dict:
    if not isinstance(response, dict):
        return {}
    market = response.get("market", response)
    return market if isinstance(market, dict) else {}


def classify(market_response: Any, orders: Iterable[dict]) -> tuple[str, str]:
    market = _market_payload(market_response)
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or market.get("settlement_value") or "").lower()
    if result in {"yes", "no", "1", "0"} or status in {"settled", "finalized"}:
        return "settled", "exchange market has an authoritative settlement/result"
    if status in {"expired", "closed"}:
        return "expired", "exchange market is closed/expired with no account position"
    # Local orders are diagnostic context only. They cannot prove a manual
    # close unless independently matched to authoritative exchange data.
    terminal_sells = [order for order in orders
                      if str(order.get("action")).lower() == "sell"
                      and str(order.get("state")).lower() == "fully_filled"]
    suffix = " despite a local filled-sell record" if terminal_sells else ""
    return "orphaned", (
        "local live position has no exchange position or independently verified close" + suffix
    )


def build_plan(db_path: Path, candidates: list[dict], markets: dict[str, Any]) -> dict:
    repairs = []
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        for position in candidates:
            try:
                orders = [dict(row) for row in db.execute(
                    "SELECT action, state, exchange_order_id, filled_quantity FROM orders "
                    "WHERE position_id = ? ORDER BY id", (position["id"],)
                ).fetchall()]
            except sqlite3.OperationalError:
                orders = []
            classification, reason = classify(markets.get(position["market_id"], {}), orders)
            alert_kind = f"position_mismatch_{str(position['side']).lower()}"
            alert_ids = [row[0] for row in db.execute("""
                SELECT id FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity = 'critical'
                  AND market_id = ? AND kind = ? ORDER BY id
            """, (position["market_id"], alert_kind)).fetchall()]
            repairs.append({
                "position_id": position["id"],
                "market_id": position["market_id"],
                "side": str(position["side"]).upper(),
                "classification": classification,
                "why_it_remained_open": reason,
                "changes": {
                    "status": {"before": position["status"], "after": "closed"},
                    "open_quantity": {"before": position["open_quantity"], "after": 0.0},
                    "reconciliation_status": {
                        "before": position["reconciliation_status"],
                        "after": f"exchange_zero_{classification}",
                    },
                    "last_reconciled_at": {
                        "before": position["last_reconciled_at"],
                        "after": "<apply_timestamp>",
                    },
                },
                "alert_changes": [
                    {"alert_id": alert_id, "resolved_at": {
                        "before": None, "after": "<apply_timestamp>"
                    }} for alert_id in alert_ids
                ],
            })
    canonical = json.dumps(repairs, sort_keys=True, separators=(",", ":"))
    return {"repairs": repairs, "plan_id": hashlib.sha256(canonical.encode()).hexdigest()}


def ensure_audit_table(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS local_reconciliation_repair_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            position_id INTEGER NOT NULL,
            market_id TEXT NOT NULL,
            side TEXT NOT NULL,
            classification TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            UNIQUE(plan_id, position_id)
        )
    """)


def apply_plan(db_path: Path, plan: dict) -> int:
    """Apply a previously confirmed plan transactionally; exchange access is impossible here."""
    applied = 0
    now = utcnow()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        ensure_audit_table(db)
        for repair in plan["repairs"]:
            row = db.execute(
                "SELECT status, open_quantity, reconciliation_status FROM positions WHERE id = ?",
                (repair["position_id"],),
            ).fetchone()
            if row is None:
                raise RuntimeError("planned position no longer exists")
            before = dict(row)
            expected = {name: change["before"] for name, change in repair["changes"].items()
                        if name != "last_reconciled_at"}
            after = {name: change["after"] for name, change in repair["changes"].items()
                     if name != "last_reconciled_at"}
            if before == after:
                continue
            if before != expected:
                raise RuntimeError("position changed after preview; generate a new preview")
            alert_ids = [item["alert_id"] for item in repair["alert_changes"]]
            has_alerts = db.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'reconciliation_alerts'
            """).fetchone() is not None
            alert_before = [dict(item) for item in db.execute(
                f"SELECT id, resolved_at FROM reconciliation_alerts WHERE id IN ({','.join('?' for _ in alert_ids)}) ORDER BY id",
                alert_ids,
            ).fetchall()] if alert_ids and has_alerts else []
            db.execute("""
                UPDATE positions SET status = ?, open_quantity = ?,
                    reconciliation_status = ?, last_reconciled_at = ? WHERE id = ?
            """, (after["status"], after["open_quantity"],
                  after["reconciliation_status"], now, repair["position_id"]))
            db.execute("""
                INSERT OR IGNORE INTO local_reconciliation_repair_audit
                (plan_id, position_id, market_id, side, classification,
                 before_json, after_json, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (plan["plan_id"], repair["position_id"], repair["market_id"],
                  repair["side"], repair["classification"],
                  json.dumps({"position": before, "alerts": alert_before}, sort_keys=True),
                  json.dumps({"position": {**after, "last_reconciled_at": now},
                              "alerts": [{"id": item["alert_id"], "resolved_at": now}
                                         for item in repair["alert_changes"]]}, sort_keys=True), now))
            if has_alerts:
                db.execute("""
                    UPDATE reconciliation_alerts SET resolved_at = ?
                    WHERE resolved_at IS NULL AND severity = 'critical' AND market_id = ?
                      AND kind = ?
                """, (now, repair["market_id"], f"position_mismatch_{repair['side'].lower()}"))
            applied += 1
        db.commit()
    return applied


def unresolved_critical_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as db:
        if db.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'reconciliation_alerts'
        """).fetchone() is None:
            return 0
        row = db.execute("""
            SELECT COUNT(*) FROM reconciliation_alerts
            WHERE resolved_at IS NULL AND severity = 'critical'
        """).fetchone()
    return int(row[0])

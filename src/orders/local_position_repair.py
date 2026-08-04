"""Local-only, exchange-authoritative repair planning for stale positions."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


ACTIVE_ORDER_STATES = {"accepted", "pending", "open", "resting", "partially_filled"}
VERIFIED_RESULTS = {"yes", "no", "1", "0"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def remote_position_map(response: Any) -> dict[tuple[str, str], float]:
    """Strictly parse a complete Kalshi positions response or fail closed."""
    if not isinstance(response, dict):
        raise ValueError("positions response must be an object")
    keys = [key for key in ("market_positions", "positions") if key in response]
    if len(keys) != 1 or not isinstance(response[keys[0]], list):
        raise ValueError("positions response must contain exactly one positions list")
    if response.get("cursor") not in (None, ""):
        raise ValueError("positions response is paginated or incomplete")
    result: dict[tuple[str, str], Decimal] = {}
    for index, row in enumerate(response[keys[0]]):
        if not isinstance(row, dict):
            raise ValueError(f"position row {index} must be an object")
        ticker = str(row.get("ticker") or row.get("market_ticker") or "").strip()
        if not ticker:
            raise ValueError(f"position row {index} is missing ticker")
        if "position_fp" in row:
            raw_quantity = row["position_fp"]
        elif "position" in row:
            raw_quantity = row["position"]
        else:
            raise ValueError(f"position row {index} is missing quantity")
        try:
            quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError(f"position row {index} has invalid quantity") from None
        if not quantity.is_finite():
            raise ValueError(f"position row {index} has invalid quantity")
        if quantity == 0:
            continue
        key = (ticker, "YES" if quantity > 0 else "NO")
        result[key] = result.get(key, Decimal("0")) + abs(quantity)
    return {key: float(quantity) for key, quantity in result.items()}


def active_order_snapshot(orders: Iterable[Any]) -> list[dict]:
    """Normalize only authoritative active orders into stable snapshot fields."""
    result = []
    for order in orders:
        if isinstance(order, dict):
            value = order
            order_id = str(value.get("order_id") or "").strip()
            market_id = str(value.get("market_id") or value.get("ticker") or "").strip()
            side = str(value.get("side") or "").upper()
            state = str(value.get("status") or value.get("state") or "").lower()
        else:
            order_id = str(getattr(order, "order_id", "")).strip()
            market_id = str(getattr(order, "market_id", "")).strip()
            raw = getattr(order, "raw", {})
            raw_side = raw.get("side") if isinstance(raw, dict) else None
            side = str(raw_side or getattr(order, "side", "")).upper()
            state = str(getattr(order, "status", "")).lower()
        if state not in ACTIVE_ORDER_STATES:
            continue
        if not order_id or not market_id or side not in {"YES", "NO"}:
            raise ValueError("active order response is malformed")
        result.append({
            "order_id": order_id, "market_id": market_id, "side": side, "state": state,
        })
    return sorted(result, key=lambda row: (row["market_id"], row["side"], row["order_id"]))


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
    mismatch_keys = {
        key for key, quantity in totals.items()
        if key in alert_keys and quantity and remote.get(key, 0.0) == 0.0
    }
    return [dict(row) for row in rows
            if (row["market_id"], str(row["side"]).upper()) in mismatch_keys]


def _market_payload(response: Any) -> dict:
    if not isinstance(response, dict):
        raise ValueError("market response must be an object")
    market = response.get("market", response)
    if not isinstance(market, dict):
        raise ValueError("market payload must be an object")
    return market


def classify(market_response: Any) -> tuple[str, str, bool]:
    market = _market_payload(market_response)
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or market.get("settlement_value") or "").lower()
    if status in {"settled", "finalized"}:
        return "settled", "exchange market is authoritatively settled", True
    if status == "expired":
        return "expired", "exchange market is authoritatively expired", True
    if status == "closed" and result in VERIFIED_RESULTS:
        return "closed_with_result", "exchange market is closed with a verified result", True
    return "manual_review", "market is active or lacks an authoritative terminal result", False


def _local_orders(
    db: sqlite3.Connection, position_id: int, market_id: str, side: str,
) -> list[dict]:
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
        if {"market_id", "side"}.issubset(columns):
            rows = db.execute("""
                SELECT id, state FROM orders
                WHERE position_id = ? OR (market_id = ? AND upper(side) = ?)
                ORDER BY id
            """, (position_id, market_id, side)).fetchall()
        else:
            rows = db.execute(
                "SELECT id, state FROM orders WHERE position_id = ? ORDER BY id", (position_id,)
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"id": int(row[0]), "state": str(row[1])} for row in rows]


def _alert_ids(db: sqlite3.Connection, market_id: str, side: str) -> list[int]:
    return [int(row[0]) for row in db.execute("""
        SELECT id FROM reconciliation_alerts
        WHERE resolved_at IS NULL AND severity = 'critical'
          AND market_id = ? AND kind = ? ORDER BY id
    """, (market_id, f"position_mismatch_{side.lower()}")).fetchall()]


def market_snapshot(markets: dict[str, Any]) -> dict[str, dict]:
    result = {}
    for ticker, response in sorted(markets.items()):
        market = _market_payload(response)
        result[ticker] = {
            "status": str(market.get("status") or "").lower(),
            "result": str(market.get("result") or market.get("settlement_value") or "").lower(),
        }
    return result


def build_plan(
    db_path: Path, candidates: list[dict], markets: dict[str, Any],
    active_orders: Iterable[Any] = (), remote_positions: dict[tuple[str, str], float] | None = None,
) -> dict:
    active = active_order_snapshot(active_orders)
    active_keys = {(row["market_id"], row["side"]) for row in active}
    counts: dict[tuple[str, str], int] = {}
    for position in candidates:
        key = (position["market_id"], str(position["side"]).upper())
        counts[key] = counts.get(key, 0) + 1
    repairs, manual = [], []
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        for position in candidates:
            side = str(position["side"]).upper()
            key = (position["market_id"], side)
            classification, reason, terminal = classify(markets.get(position["market_id"], {}))
            blockers = []
            if key in active_keys:
                blockers.append("authoritative active order exists for market and side")
            if counts[key] > 1:
                blockers.append("duplicate local positions require manual review")
            if not terminal:
                blockers.append(reason)
            orders = _local_orders(db, position["id"], position["market_id"], side)
            alert_ids = _alert_ids(db, position["market_id"], side)
            before = {
                "id": int(position["id"]), "market_id": position["market_id"],
                "side": side, "live": int(position["live"]),
                "quantity": position["quantity"], "open_quantity": position["open_quantity"],
                "status": position["status"],
                "reconciliation_status": position["reconciliation_status"],
            }
            base = {
                "position_id": int(position["id"]), "market_id": position["market_id"],
                "side": side, "classification": classification,
                "related_orders": orders, "alert_ids": alert_ids,
                "position_before": before,
            }
            if blockers:
                manual.append({**base, "manual_review_reasons": blockers})
                continue
            repairs.append({
                **base,
                "why_it_remained_open": reason,
                "changes": {
                    "status": {"before": position["status"], "after": "administratively_reconciled"},
                    "open_quantity": {"before": position["open_quantity"], "after": 0.0},
                    "reconciliation_status": {
                        "before": position["reconciliation_status"],
                        "after": f"administratively_reconciled_exchange_zero_{classification}",
                    },
                    "last_reconciled_at": {
                        "before": position["last_reconciled_at"], "after": "<apply_timestamp>",
                    },
                },
            })
    exchange_snapshot = {
        "positions": sorted([
            {"market_id": key[0], "side": key[1], "quantity": quantity}
            for key, quantity in (remote_positions or {}).items()
        ], key=lambda row: (row["market_id"], row["side"])),
        "active_orders": active,
        "markets": market_snapshot(markets),
    }
    canonical = json.dumps(
        {"repairs": repairs, "manual_review": manual, "exchange_snapshot": exchange_snapshot},
        sort_keys=True, separators=(",", ":"),
    )
    return {
        "repairs": repairs, "manual_review": manual,
        "exchange_snapshot": exchange_snapshot,
        "plan_id": hashlib.sha256(canonical.encode()).hexdigest(),
    }


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
    """Apply a revalidated plan transactionally; exchange access is impossible here."""
    applied = 0
    now = utcnow()
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        ensure_audit_table(db)
        for repair in plan["repairs"]:
            row = db.execute("""
                SELECT id, market_id, side, live, quantity, open_quantity, status,
                       reconciliation_status FROM positions WHERE id = ?
            """, (repair["position_id"],)).fetchone()
            if row is None:
                raise RuntimeError("planned position no longer exists")
            before = dict(row)
            expected = repair["position_before"]
            after_status = repair["changes"]["status"]["after"]
            after_reconciliation = repair["changes"]["reconciliation_status"]["after"]
            already_applied = (
                before["status"] == after_status and float(before["open_quantity"] or 0) == 0
                and before["reconciliation_status"] == after_reconciliation
            )
            if already_applied:
                continue
            if before != expected:
                raise RuntimeError("position changed after preview; generate a new preview")
            current_orders = _local_orders(
                db, repair["position_id"], repair["market_id"], repair["side"]
            )
            if current_orders != repair["related_orders"]:
                raise RuntimeError("related orders changed after preview; generate a new preview")
            current_alert_ids = _alert_ids(db, repair["market_id"], repair["side"])
            if current_alert_ids != repair["alert_ids"]:
                raise RuntimeError("reconciliation alerts changed after preview; generate a new preview")
            if not current_alert_ids:
                raise RuntimeError("approved plan contains no exact reconciliation alert")
            alert_before = [{"id": alert_id, "resolved_at": None} for alert_id in current_alert_ids]
            db.execute("""
                UPDATE positions SET status = ?, open_quantity = ?,
                    reconciliation_status = ?, last_reconciled_at = ? WHERE id = ?
            """, (after_status, 0.0, after_reconciliation, now, repair["position_id"]))
            db.execute("""
                INSERT OR IGNORE INTO local_reconciliation_repair_audit
                (plan_id, position_id, market_id, side, classification,
                 before_json, after_json, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                plan["plan_id"], repair["position_id"], repair["market_id"],
                repair["side"], repair["classification"],
                json.dumps({"position": before, "alerts": alert_before}, sort_keys=True),
                json.dumps({
                    "position": {
                        **before, "status": after_status, "open_quantity": 0.0,
                        "reconciliation_status": after_reconciliation,
                        "last_reconciled_at": now,
                    },
                    "alerts": [{"id": alert_id, "resolved_at": now}
                               for alert_id in current_alert_ids],
                    "invented_fill": False, "invented_exit_price": False,
                    "invented_realized_pnl": False,
                }, sort_keys=True), now,
            ))
            placeholders = ",".join("?" for _ in current_alert_ids)
            updated = db.execute(
                f"UPDATE reconciliation_alerts SET resolved_at = ? "
                f"WHERE resolved_at IS NULL AND id IN ({placeholders})",
                (now, *current_alert_ids),
            )
            if updated.rowcount != len(current_alert_ids):
                raise RuntimeError("exact reconciliation alerts could not be resolved")
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

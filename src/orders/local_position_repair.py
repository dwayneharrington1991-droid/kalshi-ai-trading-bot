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


def exchange_history_snapshot(records: Iterable[Any], record_type: str) -> list[dict]:
    """Normalize identity-only order/fill history for fail-closed repair evidence."""
    if record_type not in {"order", "fill"}:
        raise ValueError("record_type must be order or fill")
    result = []
    for record in records:
        market_id = str(getattr(record, "market_id", "") or "").strip()
        order_id = str(getattr(record, "order_id", "") or "").strip()
        if not market_id or not order_id:
            raise ValueError(f"{record_type} history response is malformed")
        row = {"market_id": market_id, "order_id": order_id}
        if record_type == "order":
            row.update({
                "client_order_id": str(getattr(record, "client_order_id", "") or ""),
                "status": str(getattr(record, "status", "") or "").lower(),
            })
        else:
            fill_id = str(getattr(record, "fill_id", "") or "").strip()
            if not fill_id:
                raise ValueError("fill history response is malformed")
            row["fill_id"] = fill_id
        result.append(row)
    return sorted(result, key=lambda row: tuple(str(row[key]) for key in sorted(row)))


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
    exchange_orders: Iterable[Any] = (), exchange_fills: Iterable[Any] = (),
    preview_paper_simulation_alert_ids: Iterable[int] = (), log_dir: Path | None = None,
) -> dict:
    active = active_order_snapshot(active_orders)
    order_history = exchange_history_snapshot(exchange_orders, "order")
    fill_history = exchange_history_snapshot(exchange_fills, "fill")
    approved_paper_alerts = {int(value) for value in preview_paper_simulation_alert_ids}
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
            orders = _local_orders(db, position["id"], position["market_id"], side)
            alert_ids = _alert_ids(db, position["market_id"], side)
            paper_approved = bool(alert_ids) and set(alert_ids) <= approved_paper_alerts
            if paper_approved:
                market = _market_payload(markets.get(position["market_id"], {}))
                if str(market.get("ticker") or position["market_id"]) != position["market_id"]:
                    blockers.append("authoritative market identity changed")
                if orders:
                    blockers.append("local exchange order history exists")
                if any(row["market_id"] == position["market_id"] for row in order_history):
                    blockers.append("authoritative exchange order history exists")
                if any(row["market_id"] == position["market_id"] for row in fill_history):
                    blockers.append("authoritative exchange fill history exists")
                for table in ("order_fills", "position_fill_projections", "order_state_events", "trade_logs"):
                    try:
                        if table == "trade_logs":
                            count = db.execute(
                                "SELECT COUNT(*) FROM trade_logs WHERE market_id = ?",
                                (position["market_id"],),
                            ).fetchone()[0]
                        else:
                            count = db.execute(f"""
                                SELECT COUNT(*) FROM {table} item
                                JOIN orders linked ON linked.id = item.order_id
                                WHERE linked.market_id = ?
                            """, (position["market_id"],)).fetchone()[0]
                    except sqlite3.OperationalError:
                        count = 0
                    if count:
                        blockers.append(f"local {table} history exists")
                log_text = ""
                if log_dir and log_dir.is_dir():
                    for path in sorted(log_dir.glob("*.log")):
                        try:
                            text = path.read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            continue
                        if position["market_id"] in text:
                            log_text += text
                required = (
                    f"live_mode=False for market {position['market_id']}",
                    f"PAPER TRADE SIMULATED for {position['market_id']}",
                )
                if not all(value in log_text for value in required):
                    blockers.append("contemporaneous paper-simulation log evidence is incomplete")
                if not blockers:
                    classification = "paper_simulation"
                    reason = "contemporaneous logs prove a paper simulation misclassified as live"
                    terminal = True
            if not terminal:
                blockers.append(reason)
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
            reconciliation_after = (
                "administratively_reconciled_paper_simulation"
                if classification == "paper_simulation"
                else f"administratively_reconciled_exchange_zero_{classification}"
            )
            repairs.append({
                **base,
                "why_it_remained_open": reason,
                "changes": {
                    "status": {"before": position["status"], "after": "administratively_reconciled"},
                    "open_quantity": {"before": position["open_quantity"], "after": 0.0},
                    "reconciliation_status": {
                        "before": position["reconciliation_status"],
                        "after": reconciliation_after,
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
        "orders": order_history,
        "fills": fill_history,
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


def build_account_activity_repairs(
    db_path: Path, remote_positions: dict[tuple[str, str], float],
    exchange_orders: Iterable[Any], exchange_fills: Iterable[Any], markets: dict[str, Any],
    manual_positions: Iterable[tuple[str, str]] = (), terminal_order_alert_ids: Iterable[int] = (),
) -> dict:
    """Plan explicit manual-position acknowledgements and terminal uncertain intents."""
    order_history = exchange_history_snapshot(exchange_orders, "order")
    fill_history = exchange_history_snapshot(exchange_fills, "fill")
    repairs, manual_review = [], []
    now_marker = "<apply_timestamp>"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        for market_id, raw_side in sorted(set(manual_positions)):
            side = raw_side.upper()
            quantity = remote_positions.get((market_id, side))
            alert = db.execute("""
                SELECT id, expected_value, observed_value FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity = 'critical' AND market_id = ?
                  AND kind = ? ORDER BY id DESC LIMIT 1
            """, (market_id, f"position_mismatch_{side.lower()}")).fetchone()
            related_orders = [row for row in order_history if row["market_id"] == market_id]
            related_fills = [row for row in fill_history if row["market_id"] == market_id]
            blockers = []
            if quantity is None or quantity <= 0:
                blockers.append("authoritative manual exchange position is absent")
            if not related_orders or not related_fills:
                blockers.append("complete exchange order/fill evidence is absent")
            if alert is None:
                blockers.append("exact unresolved position-mismatch alert is absent")
            existing = db.execute("""
                SELECT id, quantity, status FROM external_account_positions
                WHERE market_id = ? AND side = ? AND status = 'active'
            """, (market_id, side)).fetchone()
            base = {
                "repair_type": "manual_exchange_position", "market_id": market_id,
                "side": side, "quantity": quantity, "alert_id": int(alert["id"]) if alert else None,
                "alert_before": dict(alert) if alert else None,
                "exchange_orders": related_orders, "exchange_fills": related_fills,
            }
            if any(row["status"] in ACTIVE_ORDER_STATES for row in related_orders):
                blockers.append("authoritative active order exists for manual position")
            if existing and float(existing["quantity"]) != float(quantity or 0):
                blockers.append("existing manual-position acknowledgement differs")
            if blockers:
                manual_review.append({**base, "manual_review_reasons": blockers})
            else:
                repairs.append({
                    **base, "record_before": dict(existing) if existing else None,
                    "record_after": {
                        "market_id": market_id, "side": side, "quantity": quantity,
                        "source": "manual_exchange_activity", "status": "active",
                        "last_verified_at": now_marker,
                    },
                    "invented_bot_fill": False, "invented_pnl": False,
                })

        for alert_id in sorted({int(value) for value in terminal_order_alert_ids}):
            alert = db.execute("""
                SELECT id, order_id, market_id, kind FROM reconciliation_alerts
                WHERE id = ? AND resolved_at IS NULL AND severity = 'critical'
            """, (alert_id,)).fetchone()
            order = db.execute("SELECT * FROM orders WHERE id = ?", (alert["order_id"],)).fetchone() if alert and alert["order_id"] else None
            blockers = []
            if alert is None or alert["kind"] != "live_order_verification_failed":
                blockers.append("exact unresolved live-order verification alert is absent")
            if order is None or order["state"] != "verification_failed":
                blockers.append("local order is not verification_failed")
            market_id = str(alert["market_id"] if alert else (order["market_id"] if order else ""))
            _, _, terminal = classify(markets.get(market_id, {}))
            if not terminal:
                blockers.append("market is not authoritatively terminal")
            if order:
                matches = [row for row in order_history if (
                    (order["exchange_order_id"] and row["order_id"] == order["exchange_order_id"])
                    or (order["client_order_id"] and row.get("client_order_id") == order["client_order_id"])
                )]
                fill_matches = [row for row in fill_history if order["exchange_order_id"] and row["order_id"] == order["exchange_order_id"]]
                if matches or fill_matches:
                    blockers.append("authoritative order or fill exists for uncertain submission")
            base = {
                "repair_type": "terminal_unverified_order", "alert_id": alert_id,
                "order_id": int(order["id"]) if order else None, "market_id": market_id,
                "alert_before": dict(alert) if alert else None,
            }
            if blockers:
                manual_review.append({**base, "manual_review_reasons": blockers})
            else:
                repairs.append({
                    **base, "record_before": {
                        "id": int(order["id"]), "state": order["state"],
                        "exchange_order_id": order["exchange_order_id"],
                        "client_order_id": order["client_order_id"],
                        "filled_quantity": order["filled_quantity"],
                    },
                    "record_after": {
                        "state": "expired", "exchange_status": "no_record_in_complete_history",
                        "terminal_at": now_marker,
                    },
                    "invented_fill": False, "invented_pnl": False,
                })
    canonical = json.dumps({"repairs": repairs, "manual_review": manual_review}, sort_keys=True, separators=(",", ":"))
    return {"repairs": repairs, "manual_review": manual_review, "plan_id": hashlib.sha256(canonical.encode()).hexdigest()}


def apply_account_activity_repairs(db_path: Path, plan: dict) -> int:
    """Apply exact administrative repairs atomically without any exchange capability."""
    now = utcnow()
    applied = 0
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        for repair in plan["repairs"]:
            alert = db.execute("SELECT * FROM reconciliation_alerts WHERE id = ?", (repair["alert_id"],)).fetchone()
            if alert is None or alert["resolved_at"] is not None:
                raise RuntimeError("exact approved alert changed after preview")
            for key, value in (repair.get("alert_before") or {}).items():
                if alert[key] != value:
                    raise RuntimeError("exact approved alert changed after preview")
            if repair["repair_type"] == "manual_exchange_position":
                existing = db.execute("""
                    SELECT id, quantity, status FROM external_account_positions
                    WHERE market_id=? AND side=? AND status='active'
                """, (repair["market_id"], repair["side"])).fetchone()
                if (dict(existing) if existing else None) != repair.get("record_before"):
                    raise RuntimeError("manual-position acknowledgement changed after preview")
                db.execute("""
                    INSERT INTO external_account_positions
                    (market_id, side, quantity, source, exchange_order_id, exchange_fill_id,
                     first_observed_at, last_verified_at, status, metadata_json)
                    VALUES (?, ?, ?, 'manual_exchange_activity', ?, ?, ?, ?, 'active', ?)
                    ON CONFLICT(market_id, side, status) DO UPDATE SET
                        quantity=excluded.quantity, last_verified_at=excluded.last_verified_at,
                        metadata_json=excluded.metadata_json
                """, (
                    repair["market_id"], repair["side"], repair["quantity"],
                    repair["exchange_orders"][0]["order_id"], repair["exchange_fills"][0]["fill_id"],
                    now, now, json.dumps({"bot_fill": False, "pnl": None}, sort_keys=True),
                ))
                record_type, record_id = "external_account_position", None
            else:
                row = db.execute("SELECT state, exchange_order_id, client_order_id, filled_quantity FROM orders WHERE id = ?", (repair["order_id"],)).fetchone()
                expected = repair["record_before"]
                current = {"id": repair["order_id"], **dict(row)} if row else None
                if current != expected:
                    raise RuntimeError("uncertain order changed after preview")
                db.execute("""
                    UPDATE orders SET state='expired', exchange_status='no_record_in_complete_history',
                        terminal_at=?, verification_error='administratively expired after complete history verification'
                    WHERE id=?
                """, (now, repair["order_id"]))
                db.execute("""
                    INSERT INTO order_state_events
                    (order_id, from_state, to_state, source, reason, exchange_status, created_at)
                    VALUES (?, 'verification_failed', 'expired', 'administrative_reconciliation',
                            'terminal market and no order/fill in complete exchange history',
                            'no_record_in_complete_history', ?)
                """, (repair["order_id"], now))
                record_type, record_id = "order", repair["order_id"]
            updated = db.execute("UPDATE reconciliation_alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL", (now, repair["alert_id"]))
            if updated.rowcount != 1:
                raise RuntimeError("exact approved alert could not be resolved")
            db.execute("""
                INSERT INTO reconciliation_admin_repair_audit
                (plan_id, repair_type, alert_id, local_record_type, local_record_id,
                 market_id, before_json, after_json, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (plan["plan_id"], repair["repair_type"], repair["alert_id"], record_type,
                  record_id, repair.get("market_id"), json.dumps(repair.get("record_before"), sort_keys=True),
                  json.dumps(repair["record_after"], sort_keys=True), now))
            applied += 1
        db.commit()
    return applied


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

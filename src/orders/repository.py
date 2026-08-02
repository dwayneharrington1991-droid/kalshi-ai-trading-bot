"""Transactional SQLite repository for orders, fills, and state history."""

from datetime import datetime, timezone
from math import isfinite
from typing import Any, List, Optional

import aiosqlite

from src.orders.models import Order, OrderFill
from src.orders.state_machine import OrderState, OrderStateMachine


class ReconciliationRunInProgress(RuntimeError):
    """Raised when another non-stale reconciliation run owns the database."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class OrderRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @staticmethod
    async def _configure(db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")

    async def create_order(self, order: Order, source: str = "local") -> int:
        side = order.side.upper()
        action = order.action.lower()
        order_type = order.order_type.lower()
        if side not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        if action not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        if order.requested_quantity <= 0:
            raise ValueError("requested_quantity must be positive")
        if not isfinite(order.requested_quantity):
            raise ValueError("requested_quantity must be finite")
        if not order.client_order_id.strip():
            raise ValueError("client_order_id is required")
        if not order.market_id.strip():
            raise ValueError("market_id is required")
        if not order.submission_fingerprint.strip():
            raise ValueError("submission_fingerprint is required")
        if order.filled_quantity != 0:
            raise ValueError("a locally created order cannot contain fills")
        if order.remaining_quantity not in (None, order.requested_quantity):
            raise ValueError("remaining_quantity must equal requested_quantity at creation")
        if order.limit_price is not None and (
            not isfinite(order.limit_price) or not 0 <= order.limit_price <= 1
        ):
            raise ValueError("limit_price must be between 0 and 1")
        created_at = (order.created_at or datetime.now(timezone.utc)).isoformat()
        remaining = order.requested_quantity if order.remaining_quantity is None else order.remaining_quantity
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute("""
                    INSERT INTO orders (
                        position_id, parent_order_id, client_order_id, exchange_order_id,
                        environment, strategy, market_id, side, action, order_type,
                        limit_price, requested_quantity, filled_quantity, remaining_quantity,
                        vwap_fill_price, fees, state, submission_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order.position_id, order.parent_order_id, order.client_order_id,
                    order.exchange_order_id, order.environment, order.strategy, order.market_id,
                    side, action, order_type, order.limit_price, order.requested_quantity,
                    order.filled_quantity, remaining, order.vwap_fill_price, order.fees,
                    OrderState.LOCALLY_CREATED.value, order.submission_fingerprint, created_at,
                ))
                order_id = cursor.lastrowid
                await db.execute("""
                    INSERT INTO order_state_events
                        (order_id, from_state, to_state, source, created_at)
                    VALUES (?, NULL, ?, ?, ?)
                """, (order_id, OrderState.LOCALLY_CREATED.value, source, created_at))
                await db.commit()
                return order_id
            except Exception:
                await db.rollback()
                raise

    async def store_exchange_id(
        self, order_id: int, exchange_order_id: str, raw_submit_response: Optional[str] = None
    ) -> None:
        if not isinstance(exchange_order_id, str) or not exchange_order_id.strip():
            raise ValueError("exchange_order_id is required")
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT exchange_order_id FROM orders WHERE id = ?", (order_id,)
                )
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"Order {order_id} does not exist")
                if row[0] is not None and row[0] != exchange_order_id:
                    raise ValueError("exchange_order_id is immutable once stored")
                await db.execute("""
                    UPDATE orders
                    SET exchange_order_id = ?, raw_submit_response = COALESCE(?, raw_submit_response),
                        accepted_at = COALESCE(accepted_at, ?)
                    WHERE id = ?
                """, (exchange_order_id, raw_submit_response, _utcnow(), order_id))
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def transition_state(
        self, order_id: int, target: OrderState | str, source: str,
        reason: Optional[str] = None, exchange_status: Optional[str] = None,
        raw_payload: Optional[str] = None,
    ) -> None:
        target_state = OrderStateMachine.normalize(target)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute("SELECT state FROM orders WHERE id = ?", (order_id,))
                row = await cursor.fetchone()
                if row is None:
                    raise KeyError(f"Order {order_id} does not exist")
                current = OrderStateMachine.normalize(row["state"])
                OrderStateMachine.validate(current, target_state)
                now = _utcnow()
                terminal_at = now if target_state in OrderStateMachine.TERMINAL else None
                await db.execute("""
                    UPDATE orders
                    SET state = ?, exchange_status = COALESCE(?, exchange_status),
                        terminal_at = COALESCE(?, terminal_at),
                        last_verified_at = CASE WHEN ? = 'verification_failed' THEN last_verified_at ELSE ? END,
                        verification_error = CASE WHEN ? = 'verification_failed' THEN ? ELSE NULL END,
                        submitted_at = CASE WHEN ? = 'submitted' THEN COALESCE(submitted_at, ?) ELSE submitted_at END,
                        submission_attempts = submission_attempts + CASE WHEN ? = 'submitted' THEN 1 ELSE 0 END
                    WHERE id = ?
                """, (
                    target_state.value, exchange_status, terminal_at, target_state.value, now,
                    target_state.value, reason, target_state.value, now, target_state.value, order_id,
                ))
                await db.execute("""
                    INSERT INTO order_state_events
                        (order_id, from_state, to_state, source, reason, exchange_status, created_at, raw_payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, current.value, target_state.value, source, reason,
                    exchange_status, now, raw_payload,
                ))
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def insert_fill(self, order_id: int, fill: OrderFill) -> bool:
        if not fill.exchange_fill_id.strip():
            raise ValueError("exchange_fill_id is required")
        if not fill.exchange_order_id.strip():
            raise ValueError("exchange_order_id is required")
        if not isfinite(fill.quantity) or fill.quantity <= 0:
            raise ValueError("fill quantity must be positive and finite")
        if not isfinite(fill.price) or not 0 <= fill.price <= 1:
            raise ValueError("fill price must be finite and between 0 and 1")
        if not isfinite(fill.fee) or fill.fee < 0:
            raise ValueError("fill fee must be finite and non-negative")
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                order_cursor = await db.execute(
                    "SELECT exchange_order_id, requested_quantity FROM orders WHERE id = ?",
                    (order_id,),
                )
                order_row = await order_cursor.fetchone()
                if order_row is None:
                    raise KeyError(f"Order {order_id} does not exist")
                stored_exchange_id, requested_quantity = order_row
                if stored_exchange_id is None:
                    raise ValueError("order must have an exchange_order_id before storing fills")
                if stored_exchange_id != fill.exchange_order_id:
                    raise ValueError("fill exchange_order_id does not match its order")
                duplicate = await db.execute(
                    "SELECT order_id FROM order_fills WHERE exchange_fill_id = ?",
                    (fill.exchange_fill_id,),
                )
                duplicate_row = await duplicate.fetchone()
                if duplicate_row is not None:
                    if duplicate_row[0] != order_id:
                        raise ValueError("exchange_fill_id is already attached to another order")
                    await db.rollback()
                    return False
                existing = await db.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM order_fills WHERE order_id = ?",
                    (order_id,),
                )
                existing_quantity = float((await existing.fetchone())[0])
                if existing_quantity + fill.quantity > float(requested_quantity) + 1e-9:
                    raise ValueError("fill quantity exceeds requested order quantity")
                await db.execute("""
                    INSERT INTO order_fills (
                        order_id, exchange_fill_id, exchange_trade_id, exchange_order_id,
                        quantity, price, fee, is_taker, filled_at, raw_response
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order_id, fill.exchange_fill_id, fill.exchange_trade_id,
                    fill.exchange_order_id, fill.quantity, fill.price, fill.fee,
                    fill.is_taker, fill.filled_at.isoformat(), fill.raw_response,
                ))
                totals = await db.execute("""
                    SELECT SUM(quantity), SUM(quantity * price), SUM(fee)
                    FROM order_fills WHERE order_id = ?
                """, (order_id,))
                quantity, notional, fees = await totals.fetchone()
                remaining = max(float(requested_quantity) - float(quantity), 0.0)
                await db.execute("""
                    UPDATE orders SET filled_quantity = ?, remaining_quantity = ?,
                        vwap_fill_price = ?, fees = ? WHERE id = ?
                """, (quantity, remaining, notional / quantity, fees or 0.0, order_id))
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def _get_orders_by_states(self, states: tuple[OrderState, ...]) -> List[dict]:
        values = tuple(state.value for state in states)
        placeholders = ",".join("?" for _ in values)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at", values
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_active_orders(self) -> List[dict]:
        return await self._get_orders_by_states(tuple(OrderStateMachine.ACTIVE))

    async def get_uncertain_orders(self) -> List[dict]:
        return await self._get_orders_by_states(tuple(OrderStateMachine.UNCERTAIN))

    async def get_active_or_uncertain_orders(self) -> List[dict]:
        # UNCERTAIN is intentionally a subset of ACTIVE today. Keep this named
        # API explicit because reconciliation callers care about both concepts.
        states = OrderStateMachine.ACTIVE | OrderStateMachine.UNCERTAIN
        return await self._get_orders_by_states(tuple(states))

    async def get_order(self, order_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_orders(self) -> List[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM orders ORDER BY created_at")
            return [dict(row) for row in await cursor.fetchall()]

    async def mark_verified(
        self, order_id: int, exchange_status: str, raw_order_response: Optional[str] = None
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute("""
                UPDATE orders SET exchange_status = ?, last_verified_at = ?,
                    verification_error = NULL,
                    raw_order_response = COALESCE(?, raw_order_response)
                WHERE id = ?
            """, (exchange_status, _utcnow(), raw_order_response, order_id))
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(f"Order {order_id} does not exist")
            await db.commit()

    async def start_reconciliation_run(self, trigger: str, stale_after_seconds: int = 300) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute("""
                    SELECT id, started_at FROM reconciliation_runs
                    WHERE status = 'running' ORDER BY started_at DESC LIMIT 1
                """)
                running = await cursor.fetchone()
                if running:
                    started = datetime.fromisoformat(str(running[1]).replace("Z", "+00:00"))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - started).total_seconds()
                    if age <= stale_after_seconds:
                        raise ReconciliationRunInProgress(
                            f"Reconciliation run {running[0]} is still active"
                        )
                    await db.execute("""
                        UPDATE reconciliation_runs
                        SET status = 'failed', completed_at = ?, error_count = error_count + 1,
                            summary = 'abandoned stale reconciliation run'
                        WHERE id = ?
                    """, (_utcnow(), running[0]))
                inserted = await db.execute("""
                    INSERT INTO reconciliation_runs (trigger, status, started_at)
                    VALUES (?, 'running', ?)
                """, (trigger, _utcnow()))
                await db.commit()
                return inserted.lastrowid
            except Exception:
                await db.rollback()
                raise

    async def finish_reconciliation_run(self, run_id: int, status: str, **counts: Any) -> None:
        allowed = {
            "local_orders_checked", "remote_orders_seen", "fills_seen", "positions_checked",
            "mismatch_count", "error_count", "checkpoint", "summary",
        }
        unknown = set(counts) - allowed
        if unknown:
            raise ValueError(f"Unknown reconciliation fields: {sorted(unknown)}")
        assignments = ["status = ?", "completed_at = ?"]
        values: List[Any] = [status, _utcnow()]
        for key, value in counts.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(run_id)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                f"UPDATE reconciliation_runs SET {', '.join(assignments)} WHERE id = ?", values
            )
            if cursor.rowcount != 1:
                await db.rollback()
                raise KeyError(f"Reconciliation run {run_id} does not exist")
            await db.commit()

    async def record_alert(
        self, severity: str, kind: str, *, order_id: Optional[int] = None,
        position_id: Optional[int] = None, market_id: Optional[str] = None,
        expected_value: Optional[str] = None, observed_value: Optional[str] = None,
        details: Optional[str] = None,
    ) -> int:
        now = _utcnow()
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute("""
                    SELECT id FROM reconciliation_alerts
                    WHERE resolved_at IS NULL AND kind = ?
                      AND COALESCE(order_id, -1) = COALESCE(?, -1)
                      AND COALESCE(position_id, -1) = COALESCE(?, -1)
                      AND COALESCE(market_id, '') = COALESCE(?, '')
                    LIMIT 1
                """, (kind, order_id, position_id, market_id))
                row = await cursor.fetchone()
                if row:
                    alert_id = row[0]
                    await db.execute("""
                        UPDATE reconciliation_alerts SET severity = ?, expected_value = ?,
                            observed_value = ?, details = ?, last_seen_at = ?,
                            occurrence_count = occurrence_count + 1 WHERE id = ?
                    """, (
                        severity, expected_value, observed_value, details, now, alert_id,
                    ))
                else:
                    inserted = await db.execute("""
                        INSERT INTO reconciliation_alerts (
                            severity, kind, order_id, position_id, market_id,
                            expected_value, observed_value, details, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        severity, kind, order_id, position_id, market_id,
                        expected_value, observed_value, details, now, now,
                    ))
                    alert_id = inserted.lastrowid
                await db.commit()
                return alert_id
            except Exception:
                await db.rollback()
                raise

    async def get_live_position_snapshot(self) -> List[dict]:
        """Read-only position data used solely for mismatch detection."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT id, market_id, side, quantity, status, live
                FROM positions WHERE live = 1 AND status IN ('open', 'pending')
            """)
            return [dict(row) for row in await cursor.fetchall()]

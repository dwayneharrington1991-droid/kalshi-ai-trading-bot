"""Transactional SQLite repository for orders, fills, and state history."""

from datetime import datetime, timezone, timedelta
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
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                duplicate = await db.execute(
                    "SELECT id FROM orders WHERE submission_fingerprint = ? LIMIT 1",
                    (order.submission_fingerprint,),
                )
                if await duplicate.fetchone():
                    raise ValueError("submission_fingerprint already exists")
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

    async def insert_fill(
        self, order_id: int, fill: OrderFill, *, project_position: bool = False,
    ) -> bool:
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
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                order_cursor = await db.execute(
                    "SELECT exchange_order_id, requested_quantity, position_id FROM orders WHERE id = ?",
                    (order_id,),
                )
                order_row = await order_cursor.fetchone()
                if order_row is None:
                    raise KeyError(f"Order {order_id} does not exist")
                stored_exchange_id, requested_quantity, position_id = order_row
                if stored_exchange_id is None:
                    raise ValueError("order must have an exchange_order_id before storing fills")
                if stored_exchange_id != fill.exchange_order_id:
                    raise ValueError("fill exchange_order_id does not match its order")
                duplicate = await db.execute(
                    """SELECT order_id, exchange_trade_id, exchange_order_id,
                              quantity, price, fee, is_taker, filled_at
                       FROM order_fills WHERE exchange_fill_id = ?""",
                    (fill.exchange_fill_id,),
                )
                duplicate_row = await duplicate.fetchone()
                if duplicate_row is not None:
                    if duplicate_row[0] != order_id:
                        raise ValueError("exchange_fill_id is already attached to another order")
                    expected = (
                        fill.exchange_trade_id, fill.exchange_order_id, float(fill.quantity),
                        float(fill.price), float(fill.fee), fill.is_taker,
                        fill.filled_at.isoformat(),
                    )
                    observed = (
                        duplicate_row[1], duplicate_row[2], float(duplicate_row[3]),
                        float(duplicate_row[4]), float(duplicate_row[5]), duplicate_row[6],
                        duplicate_row[7],
                    )
                    if observed != expected:
                        raise ValueError("conflicting payload for existing exchange_fill_id")
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
                if project_position and position_id is not None:
                    await self._project_authoritative_fills_in_transaction(db, position_id)
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def _project_authoritative_fills_in_transaction(
        self, db: aiosqlite.Connection, position_id: int,
    ) -> int:
        position = await (await db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )).fetchone()
        if position is None:
            raise KeyError(f"Position {position_id} does not exist")
        baseline = await (await db.execute(
            "SELECT * FROM position_projection_baselines WHERE position_id = ?",
            (position_id,),
        )).fetchone()
        if baseline is None:
            base_quantity = float(
                (position["open_quantity"] if position["open_quantity"] is not None
                 else position["quantity"]) if position["live"] else 0
            )
            base_price = float(position["entry_price"]) if base_quantity else None
            await db.execute("""
                INSERT INTO position_projection_baselines
                    (position_id, base_quantity, base_entry_price, captured_at,
                     legacy_unreconciled)
                VALUES (?, ?, ?, ?, ?)
            """, (position_id, base_quantity, base_price, _utcnow(), bool(base_quantity)))
        pending = await db.execute("""
            SELECT f.exchange_fill_id, f.quantity, f.price, f.fee,
                   o.id AS order_id, o.action, o.side
            FROM order_fills f JOIN orders o ON o.id = f.order_id
            LEFT JOIN position_fill_projections p ON p.exchange_fill_id = f.exchange_fill_id
            WHERE o.position_id = ? AND p.id IS NULL
            ORDER BY f.filled_at, f.exchange_fill_id
        """, (position_id,))
        inserted = 0
        for fill in await pending.fetchall():
            if fill["side"] != str(position["side"]).upper():
                raise ValueError("fill side is inconsistent with position")
            await db.execute("""
                INSERT INTO position_fill_projections
                    (position_id, order_id, exchange_fill_id, action,
                     quantity, price, fee, applied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (position_id, fill["order_id"], fill["exchange_fill_id"],
                  fill["action"], fill["quantity"], fill["price"], fill["fee"], _utcnow()))
            inserted += 1
        baseline = await (await db.execute(
            "SELECT base_quantity, base_entry_price FROM position_projection_baselines WHERE position_id = ?",
            (position_id,),
        )).fetchone()
        totals = await (await db.execute("""
            SELECT
              COALESCE(SUM(CASE WHEN action='buy' THEN quantity ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN action='buy' THEN quantity * price ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN action='sell' THEN quantity ELSE 0 END), 0),
              COALESCE(SUM(CASE WHEN action='sell' THEN quantity * price ELSE 0 END), 0)
            FROM position_fill_projections WHERE position_id = ?
        """, (position_id,))).fetchone()
        bought, buy_notional, sold, sell_notional = map(float, totals)
        if not bought and not sold:
            return inserted
        base_quantity, base_price = float(baseline[0]), baseline[1]
        open_quantity = base_quantity + bought - sold
        if open_quantity < -1e-9:
            raise ValueError("authoritative sell fills over-close position")
        entry_quantity = base_quantity + bought
        entry_notional = base_quantity * float(base_price or 0) + buy_notional
        average_entry = entry_notional / entry_quantity if entry_quantity else None
        average_exit = sell_notional / sold if sold else None
        open_quantity = max(open_quantity, 0.0)
        await db.execute("""
            UPDATE positions SET quantity = ?, open_quantity = ?, filled_quantity = ?,
                entry_price = COALESCE(?, entry_price), average_entry_price = ?,
                average_exit_price = ?, live = ?, status = ?, last_reconciled_at = ?,
                reconciliation_status = 'verified' WHERE id = ?
        """, (open_quantity, open_quantity, entry_quantity, average_entry,
              average_entry, average_exit, open_quantity > 0,
              "open" if open_quantity > 0 else "closed", _utcnow(), position_id))
        return inserted

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

    async def get_order_by_fingerprint(self, fingerprint: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT * FROM orders WHERE submission_fingerprint = ?
                ORDER BY created_at DESC LIMIT 1
            """, (fingerprint,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_active_orders_for_position(self, position_id: int) -> List[dict]:
        states = tuple(
            state.value for state in (OrderStateMachine.ACTIVE | OrderStateMachine.UNCERTAIN)
        )
        placeholders = ",".join("?" for _ in states)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM orders WHERE position_id = ? AND state IN ({placeholders}) ORDER BY id",
                (position_id, *states),
            )).fetchall()
            return [dict(row) for row in rows]

    async def assert_reconciliation_healthy(self, max_age_seconds: int) -> None:
        health = await self.get_reconciliation_health(max_age_seconds)
        if not health["healthy_status"]:
            raise RuntimeError(health["health_reason"])
        if not health["fresh"]:
            raise RuntimeError(health["freshness_reason"])
        if health["critical_alert_count"]:
            raise RuntimeError("unresolved critical reconciliation alert")

    async def get_reconciliation_health(self, max_age_seconds: int) -> dict:
        """Return the detailed checkpoint state used by execution and diagnostics."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute("""
                SELECT status, completed_at FROM reconciliation_runs
                WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1
            """)
            row = await cursor.fetchone()
            healthy_status = row is not None and row[0] in {"completed", "completed_with_mismatches"}
            age_seconds = None
            fresh = False
            if row is not None:
                try:
                    completed = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=timezone.utc)
                    age_seconds = max(0.0, (datetime.now(timezone.utc) - completed).total_seconds())
                    fresh = age_seconds <= max_age_seconds
                except (TypeError, ValueError):
                    pass
            critical = await db.execute("""
                SELECT COUNT(*) FROM reconciliation_alerts
                WHERE resolved_at IS NULL AND severity = 'critical'
            """)
            critical_count = int((await critical.fetchone())[0])
            return {
                "status": row[0] if row else None,
                "healthy_status": healthy_status,
                "health_reason": ("latest checkpoint completed" if healthy_status
                                  else "reconciliation has no completed health checkpoint"),
                "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                "fresh": fresh,
                "freshness_reason": ("checkpoint is fresh" if fresh
                                     else "reconciliation health checkpoint is stale"),
                "critical_alert_count": critical_count,
            }

    async def assert_position_intent(
        self, position_id: int, market_id: str, side: str,
        action: str, quantity: float,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            )).fetchone()
            if row is None:
                raise RuntimeError("position does not exist")
            if row["market_id"] != market_id or str(row["side"]).upper() != side.upper():
                raise RuntimeError("order intent does not match its position")
            if action.lower() == "sell":
                available = row["open_quantity"] if row["open_quantity"] is not None else row["quantity"]
                if not row["live"] or float(quantity) > float(available) + 1e-9:
                    raise RuntimeError("sell intent exceeds verified open position quantity")

    async def project_authoritative_fills(self, position_id: int) -> int:
        """Apply unprojected fills and derive a position atomically."""
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            try:
                await db.execute("BEGIN IMMEDIATE")
                position_cursor = await db.execute(
                    "SELECT * FROM positions WHERE id = ?", (position_id,)
                )
                position = await position_cursor.fetchone()
                if position is None:
                    raise KeyError(f"Position {position_id} does not exist")
                baseline_cursor = await db.execute(
                    "SELECT * FROM position_projection_baselines WHERE position_id = ?",
                    (position_id,),
                )
                baseline = await baseline_cursor.fetchone()
                if baseline is None:
                    base_quantity = float(
                        (position["open_quantity"] if position["open_quantity"] is not None
                         else position["quantity"])
                        if position["live"] else 0
                    )
                    base_price = float(position["entry_price"]) if base_quantity else None
                    await db.execute("""
                        INSERT INTO position_projection_baselines
                            (position_id, base_quantity, base_entry_price, captured_at,
                             legacy_unreconciled)
                        VALUES (?, ?, ?, ?, ?)
                    """, (position_id, base_quantity, base_price, _utcnow(), bool(base_quantity)))
                pending = await db.execute("""
                    SELECT f.exchange_fill_id, f.quantity, f.price, f.fee,
                           o.id AS order_id, o.action, o.side
                    FROM order_fills f JOIN orders o ON o.id = f.order_id
                    LEFT JOIN position_fill_projections p
                      ON p.exchange_fill_id = f.exchange_fill_id
                    WHERE o.position_id = ? AND p.id IS NULL
                    ORDER BY f.filled_at, f.exchange_fill_id
                """, (position_id,))
                inserted = 0
                for fill in await pending.fetchall():
                    if fill["side"] != str(position["side"]).upper():
                        raise ValueError("fill side is inconsistent with position")
                    await db.execute("""
                        INSERT INTO position_fill_projections
                            (position_id, order_id, exchange_fill_id, action,
                             quantity, price, fee, applied_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        position_id, fill["order_id"], fill["exchange_fill_id"],
                        fill["action"], fill["quantity"], fill["price"], fill["fee"],
                        _utcnow(),
                    ))
                    inserted += 1
                baseline = await (await db.execute(
                    "SELECT base_quantity, base_entry_price FROM position_projection_baselines WHERE position_id = ?",
                    (position_id,),
                )).fetchone()
                totals = await (await db.execute("""
                    SELECT
                      COALESCE(SUM(CASE WHEN action='buy' THEN quantity ELSE 0 END), 0),
                      COALESCE(SUM(CASE WHEN action='buy' THEN quantity * price ELSE 0 END), 0),
                      COALESCE(SUM(CASE WHEN action='sell' THEN quantity ELSE 0 END), 0),
                      COALESCE(SUM(CASE WHEN action='sell' THEN quantity * price ELSE 0 END), 0)
                    FROM position_fill_projections WHERE position_id = ?
                """, (position_id,))).fetchone()
                bought, buy_notional, sold, sell_notional = map(float, totals)
                if not bought and not sold:
                    await db.commit()
                    return 0
                base_quantity, base_price = float(baseline[0]), baseline[1]
                open_quantity = base_quantity + bought - sold
                if open_quantity < -1e-9:
                    raise ValueError("authoritative sell fills over-close position")
                entry_quantity = base_quantity + bought
                entry_notional = base_quantity * float(base_price or 0) + buy_notional
                average_entry = entry_notional / entry_quantity if entry_quantity else None
                average_exit = sell_notional / sold if sold else None
                open_quantity = max(open_quantity, 0.0)
                await db.execute("""
                    UPDATE positions SET quantity = ?, open_quantity = ?, filled_quantity = ?,
                        entry_price = COALESCE(?, entry_price), average_entry_price = ?,
                        average_exit_price = ?, live = ?, status = ?,
                        last_reconciled_at = ?, reconciliation_status = 'verified'
                    WHERE id = ?
                """, (
                    open_quantity, open_quantity, entry_quantity, average_entry,
                    average_entry, average_exit, open_quantity > 0,
                    "open" if open_quantity > 0 else "closed", _utcnow(), position_id,
                ))
                await db.commit()
                return inserted
            except Exception:
                await db.rollback()
                raise

    async def get_all_orders(self) -> List[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM orders ORDER BY created_at")
            return [dict(row) for row in await cursor.fetchall()]

    async def get_matching_active_order(
        self, market_id: str, side: str, action: str
    ) -> Optional[dict]:
        states = sorted(
            OrderStateMachine.ACTIVE | {OrderState.VERIFICATION_FAILED},
            key=lambda state: state.value,
        )
        placeholders = ",".join("?" for _ in states)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM orders
                    WHERE market_id = ? AND side = ? AND action = ?
                      AND state IN ({placeholders})
                    ORDER BY id DESC LIMIT 1""",
                (market_id, side.upper(), action.lower(), *(state.value for state in states)),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_canary_risk_snapshot(
        self, market_id: str, market_data_max_age_seconds: int
    ) -> dict:
        """Return conservative local risk facts used immediately before canary submission."""
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=max(0, market_data_max_age_seconds)
        )
        today = datetime.now(timezone.utc).date().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            db.row_factory = aiosqlite.Row
            risk_cursor = await db.execute("""
                SELECT COUNT(*) AS open_positions,
                       COALESCE(SUM(entry_price * quantity), 0) AS total_risk
                FROM positions
                WHERE live = 1 AND status IN ('open', 'pending')
            """)
            risk = dict(await risk_cursor.fetchone())
            pnl_cursor = await db.execute("""
                SELECT COALESCE(SUM(pnl), 0) AS daily_pnl
                FROM trade_logs WHERE substr(exit_timestamp, 1, 10) = ?
            """, (today,))
            daily_pnl = float((await pnl_cursor.fetchone())["daily_pnl"])
            marks_cursor = await db.execute("""
                SELECT p.side, p.entry_price, p.quantity, m.yes_price, m.no_price,
                       m.last_updated
                FROM positions p LEFT JOIN markets m ON m.market_id = p.market_id
                WHERE p.live = 1 AND p.status IN ('open', 'pending')
            """)
            unrealized_pnl = 0.0
            any_open_market_stale = False
            for row in await marks_cursor.fetchall():
                mark = row["yes_price"] if str(row["side"]).upper() == "YES" else row["no_price"]
                try:
                    unrealized_pnl += (float(mark) - float(row["entry_price"])) * int(row["quantity"])
                    updated = datetime.fromisoformat(str(row["last_updated"]).replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    any_open_market_stale |= updated.astimezone(timezone.utc) < cutoff
                except (TypeError, ValueError):
                    any_open_market_stale = True
            rejection_cursor = await db.execute("""
                SELECT state FROM orders ORDER BY id DESC LIMIT ?
            """, (max(1, self._safe_limit(100)),))
            consecutive_rejections = 0
            for row in await rejection_cursor.fetchall():
                if row["state"] != OrderState.REJECTED.value:
                    break
                consecutive_rejections += 1
            market_cursor = await db.execute(
                "SELECT last_updated FROM markets WHERE market_id = ?", (market_id,)
            )
            market = await market_cursor.fetchone()
            market_data_stale = True
            if market is not None and market["last_updated"]:
                try:
                    updated = datetime.fromisoformat(str(market["last_updated"]).replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    market_data_stale = updated.astimezone(timezone.utc) < cutoff
                except (TypeError, ValueError):
                    market_data_stale = True
            return {
                "open_positions": int(risk["open_positions"]),
                "total_risk": float(risk["total_risk"]),
                "daily_pnl": daily_pnl + unrealized_pnl,
                "consecutive_rejections": consecutive_rejections,
                "market_data_stale": market_data_stale or any_open_market_stale,
            }

    @staticmethod
    def _safe_limit(value: int) -> int:
        return max(1, min(int(value), 1000))

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

    async def resolve_order_alerts(self, order_id: int, *, kinds: tuple[str, ...]) -> int:
        if not kinds:
            return 0
        placeholders = ",".join("?" for _ in kinds)
        async with aiosqlite.connect(self.db_path) as db:
            await self._configure(db)
            cursor = await db.execute(
                f"""UPDATE reconciliation_alerts SET resolved_at = ?
                    WHERE order_id = ? AND resolved_at IS NULL
                      AND kind IN ({placeholders})""",
                (_utcnow(), order_id, *kinds),
            )
            await db.commit()
            return cursor.rowcount

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

"""Exchange-authoritative order reconciliation in read-only shadow mode."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from src.orders.exchange_models import ExchangeFill, ExchangeOrder
from src.orders.models import OrderFill
from src.orders.repository import OrderRepository, ReconciliationRunInProgress
from src.orders.state_machine import OrderState, OrderStateMachine
from src.utils.logging_setup import get_trading_logger


@dataclass
class ReconciliationResult:
    run_id: Optional[int]
    status: str
    local_orders_checked: int = 0
    remote_orders_seen: int = 0
    fills_seen: int = 0
    positions_checked: int = 0
    mismatch_count: int = 0
    error_count: int = 0


class OrderReconciler:
    """Reconcile order metadata and fills without mutating trading positions."""

    def __init__(
        self, repository: OrderRepository, kalshi_client: Any, *,
        shadow_mode: bool = True, paper_mode: bool = False,
        max_staleness_seconds: int = 30, verification_timeout_seconds: int = 30,
        project_positions: bool = False,
    ):
        if not shadow_mode:
            raise ValueError("Phase 2 reconciliation is shadow-mode only")
        self.repository = repository
        self.client = kalshi_client
        self.shadow_mode = shadow_mode
        self.paper_mode = paper_mode
        self.max_staleness_seconds = max_staleness_seconds
        self.verification_timeout_seconds = verification_timeout_seconds
        self.project_positions = project_positions
        self.logger = get_trading_logger("order_reconciler")

    async def reconcile(self, trigger: str = "periodic", full: bool = True) -> ReconciliationResult:
        if self.paper_mode:
            self.logger.info("Skipping exchange reconciliation in paper mode", trigger=trigger)
            return ReconciliationResult(run_id=None, status="skipped_paper")

        try:
            run_id = await self.repository.start_reconciliation_run(
                trigger, stale_after_seconds=max(300, self.verification_timeout_seconds * 10)
            )
        except ReconciliationRunInProgress as exc:
            self.logger.warning(
                "Reconciliation skipped because another run is active",
                trigger=trigger, error=str(exc), shadow_mode=True,
            )
            return ReconciliationResult(run_id=None, status="skipped_concurrent")
        result = ReconciliationResult(run_id=run_id, status="running")
        self.logger.info(
            "Order reconciliation started", reconciliation_run_id=run_id,
            trigger=trigger, shadow_mode=True,
        )
        local_orders: List[dict] = []
        try:
            local_orders = await self.repository.get_active_or_uncertain_orders()
            all_local_orders = await self.repository.get_all_orders()
            result.local_orders_checked = len(local_orders)
            remote_orders: List[ExchangeOrder] = await self.client.get_all_orders(limit=1000)
            remote_fills: List[ExchangeFill] = await self.client.get_all_fills(limit=1000)
            result.remote_orders_seen = len(remote_orders)
            result.fills_seen = len(remote_fills)
            by_exchange = {order.order_id: order for order in remote_orders}
            by_client: Dict[str, List[ExchangeOrder]] = {}
            for order in remote_orders:
                if order.client_order_id:
                    by_client.setdefault(order.client_order_id, []).append(order)
            fills_by_order: Dict[str, List[ExchangeFill]] = {}
            for fill in remote_fills:
                fills_by_order.setdefault(fill.order_id, []).append(fill)

            matched_remote_ids = set()
            for local in local_orders:
                if local["state"] == OrderState.LOCALLY_CREATED.value:
                    continue  # No exchange submission is expected yet.
                if self._is_stale(local):
                    await self._record_mismatch(
                        result, "warning", "stale_verification", local,
                        expected=f"verified within {self.max_staleness_seconds}s",
                        observed=local.get("last_verified_at") or "never verified",
                    )
                matches: List[ExchangeOrder] = []
                if local.get("exchange_order_id"):
                    match = by_exchange.get(local["exchange_order_id"])
                    if match:
                        matches = [match]
                if not matches and local.get("client_order_id"):
                    matches = by_client.get(local["client_order_id"], [])

                if len(matches) != 1:
                    kind = "ambiguous_order" if len(matches) > 1 else "local_only_order"
                    await self._record_mismatch(
                        result, "error", kind, local,
                        expected="one authoritative Kalshi order",
                        observed=f"{len(matches)} matching orders",
                    )
                    if len(matches) > 1 or self._age_seconds(local) >= self.verification_timeout_seconds:
                        await self._fail_verification(local, kind)
                    continue

                remote = matches[0]
                matched_remote_ids.add(remote.order_id)
                await self._reconcile_order(
                    local, remote, fills_by_order.get(remote.order_id, []), result
                )

            if full:
                known_exchange_ids = {
                    order["exchange_order_id"] for order in all_local_orders
                    if order.get("exchange_order_id")
                }
                known_client_ids = {
                    order["client_order_id"] for order in all_local_orders
                    if order.get("client_order_id")
                }
                for remote in remote_orders:
                    if (
                        remote.order_id not in matched_remote_ids
                        and remote.order_id not in known_exchange_ids
                        and remote.client_order_id not in known_client_ids
                    ):
                        await self._record_mismatch(
                            result, "warning", "remote_only_order", None,
                            market_id=remote.market_id,
                            expected="matching local order",
                            observed=remote.order_id,
                        )
                await self._compare_positions(result)

            result.status = "completed_with_mismatches" if result.mismatch_count else "completed"
            await self.repository.finish_reconciliation_run(
                run_id, result.status, **self._result_counts(result)
            )
            self.logger.info(
                "Order reconciliation completed", reconciliation_run_id=run_id,
                status=result.status, mismatches=result.mismatch_count,
                fills=result.fills_seen,
            )
            return result
        except Exception as exc:
            result.status = "failed"
            result.error_count += 1
            await self.repository.record_alert(
                "critical", "reconciliation_api_failure", details=str(exc)
            )
            for local in local_orders:
                await self._fail_verification(local, "authoritative verification unavailable")
            await self.repository.finish_reconciliation_run(
                run_id, "failed", **self._result_counts(result), summary=str(exc)
            )
            self.logger.error(
                "Order reconciliation failed", reconciliation_run_id=run_id,
                error=str(exc), shadow_mode=True,
            )
            return result

    async def _reconcile_order(
        self, local: dict, remote: ExchangeOrder, fills: Iterable[ExchangeFill],
        result: ReconciliationResult,
    ) -> None:
        run_id = result.run_id
        local_id = local["id"]
        if not local.get("exchange_order_id"):
            await self.repository.store_exchange_id(
                local_id, remote.order_id, json.dumps(remote.raw, default=str)
            )
        for fill in sorted(fills, key=lambda item: (item.filled_at, item.fill_id)):
            inserted = await self.repository.insert_fill(
                local_id,
                OrderFill(
                    exchange_fill_id=fill.fill_id,
                    exchange_trade_id=fill.trade_id,
                    exchange_order_id=fill.order_id,
                    quantity=float(fill.quantity),
                    price=float(fill.price),
                    fee=float(fill.fee),
                    is_taker=fill.is_taker,
                    filled_at=fill.filled_at,
                    raw_response=json.dumps(fill.raw, default=str),
                ),
                project_position=self.project_positions,
            )
            if inserted:
                self.logger.info(
                    "Authoritative fill stored", reconciliation_run_id=run_id,
                    local_order_id=local_id, exchange_order_id=remote.order_id,
                    exchange_fill_id=fill.fill_id, quantity=str(fill.quantity),
                    price=str(fill.price), fee=str(fill.fee),
                )

        refreshed = await self.repository.get_order(local_id)
        fill_mismatch = Decimal(str(refreshed["filled_quantity"] or 0)) != remote.filled_quantity
        quantity_mismatch = Decimal(str(refreshed["requested_quantity"])) != remote.initial_quantity
        if fill_mismatch:
            await self._record_mismatch(
                result, "error", "fill_quantity_mismatch", refreshed,
                expected=str(remote.filled_quantity),
                observed=str(refreshed["filled_quantity"] or 0),
            )
        if quantity_mismatch:
            await self._record_mismatch(
                result, "error", "requested_quantity_mismatch", refreshed,
                expected=str(remote.initial_quantity),
                observed=str(refreshed["requested_quantity"]),
            )
        target = (
            OrderState.VERIFICATION_FAILED
            if fill_mismatch or quantity_mismatch
            else self._authoritative_state(remote, refreshed)
        )
        await self._transition(local_id, refreshed["state"], target, remote, run_id)
        await self.repository.mark_verified(
            local_id, remote.status, json.dumps(remote.raw, default=str)
        )

    @staticmethod
    def _authoritative_state(remote: ExchangeOrder, local: dict) -> OrderState:
        filled = Decimal(str(local["filled_quantity"] or 0))
        requested = Decimal(str(local["requested_quantity"]))
        status = remote.status
        if filled >= requested:
            return OrderState.FULLY_FILLED
        if status in {"canceled", "cancelled"}:
            now = datetime.now(timezone.utc)
            if remote.expiration_at and remote.expiration_at <= now:
                return OrderState.EXPIRED
            return OrderState.CANCELED
        if status in {"rejected"}:
            return OrderState.REJECTED
        if filled > 0:
            return OrderState.PARTIALLY_FILLED
        if status in {"resting", "open"}:
            return OrderState.RESTING
        if status in {"accepted", "pending"}:
            return OrderState.ACCEPTED
        return OrderState.VERIFICATION_FAILED

    async def _transition(
        self, order_id: int, current: str, target: OrderState,
        remote: ExchangeOrder, run_id: int,
    ) -> None:
        current_state = OrderStateMachine.normalize(current)
        if current_state == target:
            return
        # Preserve acceptance as a separate state when recovery jumps from a
        # submitted request directly to an authoritative active/final status.
        if current_state == OrderState.SUBMITTED and target not in {
            OrderState.ACCEPTED, OrderState.REJECTED, OrderState.VERIFICATION_FAILED,
        }:
            await self.repository.transition_state(
                order_id, OrderState.ACCEPTED, "reconciliation",
                exchange_status=remote.status,
            )
            current_state = OrderState.ACCEPTED
        if not OrderStateMachine.can_transition(current_state, target):
            target = OrderState.VERIFICATION_FAILED
        await self.repository.transition_state(
            order_id, target, "reconciliation",
            reason="authoritative Kalshi order status",
            exchange_status=remote.status,
            raw_payload=json.dumps(remote.raw, default=str),
        )
        self.logger.info(
            "Order state reconciled", reconciliation_run_id=run_id,
            local_order_id=order_id, exchange_order_id=remote.order_id,
            from_state=current_state.value, to_state=target.value,
            exchange_status=remote.status,
        )

    async def _fail_verification(self, local: dict, reason: str) -> None:
        current = OrderStateMachine.normalize(local["state"])
        if current in {OrderState.LOCALLY_CREATED, OrderState.VERIFICATION_FAILED}:
            return
        if OrderStateMachine.can_transition(current, OrderState.VERIFICATION_FAILED):
            await self.repository.transition_state(
                local["id"], OrderState.VERIFICATION_FAILED,
                "reconciliation", reason=reason,
            )

    async def _record_mismatch(
        self, result: ReconciliationResult, severity: str, kind: str,
        local: Optional[dict], *, market_id: Optional[str] = None,
        expected: Optional[str] = None, observed: Optional[str] = None,
    ) -> None:
        result.mismatch_count += 1
        await self.repository.record_alert(
            severity, kind, order_id=local["id"] if local else None,
            market_id=market_id or (local.get("market_id") if local else None),
            expected_value=expected, observed_value=observed,
        )
        self.logger.warning(
            "Reconciliation mismatch", reconciliation_run_id=result.run_id,
            mismatch_kind=kind, local_order_id=local["id"] if local else None,
            exchange_order_id=local.get("exchange_order_id") if local else observed,
            market_id=market_id or (local.get("market_id") if local else None),
        )

    async def _compare_positions(self, result: ReconciliationResult) -> None:
        local_positions = await self.repository.get_live_position_snapshot()
        response = await self.client.get_positions()
        remote_positions = response.get("market_positions", response.get("positions", []))
        result.positions_checked = len(local_positions)
        local_map: Dict[tuple, Decimal] = {}
        for position in local_positions:
            key = (position["market_id"], position["side"].upper())
            local_map[key] = local_map.get(key, Decimal("0")) + Decimal(str(position["quantity"]))
        remote_map: Dict[tuple, Decimal] = {}
        for position in remote_positions:
            ticker = str(position.get("ticker") or position.get("market_ticker") or "")
            raw_quantity = Decimal(str(position.get("position_fp", position.get("position", 0))))
            if raw_quantity == 0:
                continue
            key = (ticker, "YES" if raw_quantity > 0 else "NO")
            remote_map[key] = remote_map.get(key, Decimal("0")) + abs(raw_quantity)
        for key in sorted(set(local_map) | set(remote_map)):
            if local_map.get(key, Decimal("0")) != remote_map.get(key, Decimal("0")):
                await self._record_mismatch(
                    result, "critical", f"position_mismatch_{key[1].lower()}", None,
                    market_id=key[0], expected=str(local_map.get(key, Decimal("0"))),
                    observed=str(remote_map.get(key, Decimal("0"))),
                )

    def _is_stale(self, local: dict) -> bool:
        return self._age_seconds(local) > self.max_staleness_seconds

    @staticmethod
    def _age_seconds(local: dict) -> float:
        value = (
            local.get("last_verified_at") or local.get("accepted_at")
            or local.get("submitted_at") or local.get("created_at")
        )
        if not value:
            return float("inf")
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - timestamp).total_seconds()

    @staticmethod
    def _result_counts(result: ReconciliationResult) -> dict:
        return {
            "local_orders_checked": result.local_orders_checked,
            "remote_orders_seen": result.remote_orders_seen,
            "fills_seen": result.fills_seen,
            "positions_checked": result.positions_checked,
            "mismatch_count": result.mismatch_count,
            "error_count": result.error_count,
        }

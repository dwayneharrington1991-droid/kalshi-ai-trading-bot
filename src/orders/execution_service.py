"""Fail-closed live execution backed by the authoritative order ledger."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.orders.models import Order
from src.orders.reconciler import OrderReconciler
from src.orders.repository import OrderRepository
from src.orders.state_machine import OrderState, OrderStateMachine
from src.utils.logging_setup import get_trading_logger

PRODUCTION_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK"


class ExecutionSafetyError(RuntimeError):
    """Raised when an execution safety gate fails."""


@dataclass(frozen=True)
class OrderIntent:
    market_id: str
    side: str
    action: str
    quantity: float
    price: float
    order_type: str
    position_id: int
    environment: str
    strategy: Optional[str] = None

    def fingerprint(self) -> str:
        payload = "|".join((
            str(self.position_id), self.market_id.strip(), self.side.upper(),
            self.action.lower(), self.order_type.lower(),
            format(Decimal(str(self.quantity)), "f"),
            format(Decimal(str(self.price)), "f"), self.environment.lower(),
        ))
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionSafetyConfig:
    live_mode: bool = False
    authoritative_execution_enabled: bool = False
    reconciliation_enabled: bool = False
    kill_switch: bool = True
    production_acknowledgement: str = ""
    reconciliation_max_age_seconds: int = 30
    allow_risk_reducing_exits: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    order_id: Optional[int]
    client_order_id: Optional[str]
    state: str
    submitted: bool = False
    recovered: bool = False


class VerifiedExecutionService:
    """Submit once, then project positions exclusively from authoritative fills."""

    def __init__(self, repository: OrderRepository, kalshi_client: Any,
                 reconciler: OrderReconciler, safety: ExecutionSafetyConfig):
        self.repository = repository
        self.client = kalshi_client
        self.reconciler = reconciler
        self.reconciler.project_positions = True
        self.safety = safety
        self.logger = get_trading_logger("verified_execution")

    async def execute(self, intent: OrderIntent) -> ExecutionResult:
        self._validate_intent(intent)
        fingerprint = intent.fingerprint()
        existing = await self.repository.get_order_by_fingerprint(fingerprint)
        requires_submission = existing is None or existing["state"] == "locally_created"
        await self._preflight_gateway(intent, require_health=requires_submission)
        if existing is not None:
            return await self._recover(existing, intent)
        client_order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kalshi-order:{fingerprint}"))
        order_id = await self.repository.create_order(Order(
            client_order_id=client_order_id, market_id=intent.market_id,
            side=intent.side, action=intent.action, order_type=intent.order_type,
            limit_price=intent.price, requested_quantity=intent.quantity,
            submission_fingerprint=fingerprint, environment=intent.environment,
            strategy=intent.strategy, position_id=intent.position_id,
        ), source="verified_execution")
        return await self._submit(order_id, client_order_id, intent)

    async def _submit(
        self, order_id: int, client_order_id: str, intent: OrderIntent,
    ) -> ExecutionResult:
        await self._balance_gateway(intent)
        await self.repository.transition_state(
            order_id, OrderState.SUBMITTED, "verified_execution",
            reason="durably recorded before HTTP submission",
        )
        try:
            response = await self.client.place_order(**self._order_params(intent, client_order_id))
            remote = response.get("order") if isinstance(response, dict) else None
            exchange_order_id = remote.get("order_id") if isinstance(remote, dict) else None
            if not isinstance(exchange_order_id, str) or not exchange_order_id.strip():
                raise ExecutionSafetyError("submission response contains no exchange order identity")
            await self.repository.store_exchange_id(
                order_id, exchange_order_id, json.dumps(response, default=str)
            )
            await self.repository.transition_state(
                order_id, OrderState.ACCEPTED, "verified_execution",
                reason="exchange order identity received",
            )
        except Exception as exc:
            await self._quarantine(order_id, f"submission outcome unverifiable: {exc}")
            return ExecutionResult(order_id, client_order_id, "verification_failed", submitted=True)
        return await self._verify_and_project(order_id, client_order_id, submitted=True)

    async def _recover(self, existing: dict, intent: OrderIntent) -> ExecutionResult:
        state = OrderStateMachine.normalize(existing["state"])
        if state in OrderStateMachine.TERMINAL:
            try:
                await self.repository.project_authoritative_fills(existing["position_id"])
            except Exception as exc:
                await self.repository.record_alert(
                    "critical", "live_fill_projection_failed", order_id=existing["id"],
                    position_id=existing.get("position_id"), market_id=existing.get("market_id"),
                    details=str(exc),
                )
                return ExecutionResult(existing["id"], existing["client_order_id"],
                                       "verification_failed", recovered=True)
            return ExecutionResult(existing["id"], existing["client_order_id"],
                                   state.value, recovered=True)
        if state == OrderState.LOCALLY_CREATED:
            return await self._submit(
                existing["id"], existing["client_order_id"], intent
            )
        return await self._verify_and_project(existing["id"], existing["client_order_id"],
                                              submitted=False, recovered=True)

    async def _verify_and_project(self, order_id: int, client_order_id: str, *,
                                  submitted: bool, recovered: bool = False) -> ExecutionResult:
        result = await self.reconciler.reconcile(trigger="post_submission", full=False)
        if result.status not in {"completed", "completed_with_mismatches"}:
            await self._quarantine(order_id, f"verification run status: {result.status}")
        order = await self.repository.get_order(order_id)
        if order and not order.get("exchange_status"):
            await self._quarantine(order_id, "submitted order was not authoritatively verified")
            order = await self.repository.get_order(order_id)
        if order and order.get("position_id") is not None:
            try:
                await self.repository.project_authoritative_fills(order["position_id"])
            except Exception as exc:
                await self._quarantine(order_id, f"authoritative fill projection failed: {exc}")
            order = await self.repository.get_order(order_id)
        if order and order["state"] != OrderState.VERIFICATION_FAILED.value:
            await self.repository.resolve_order_alerts(
                order_id, kinds=("live_order_verification_failed",)
            )
        return ExecutionResult(order_id, client_order_id,
                               order["state"] if order else "verification_failed",
                               submitted=submitted, recovered=recovered)

    async def _quarantine(self, order_id: int, reason: str) -> None:
        order = await self.repository.get_order(order_id)
        if not order:
            return
        current = OrderStateMachine.normalize(order["state"])
        if current != OrderState.VERIFICATION_FAILED and OrderStateMachine.can_transition(
            current, OrderState.VERIFICATION_FAILED
        ):
            await self.repository.transition_state(order_id, OrderState.VERIFICATION_FAILED,
                                                   "verified_execution", reason=reason)
        await self.repository.record_alert(
            "critical", "live_order_verification_failed", order_id=order_id,
            position_id=order.get("position_id"), market_id=order.get("market_id"),
            details=reason,
        )

    async def _preflight_gateway(self, intent: OrderIntent, *, require_health: bool) -> None:
        if not self.safety.live_mode:
            raise ExecutionSafetyError("live mode is not enabled")
        if not self.safety.authoritative_execution_enabled:
            raise ExecutionSafetyError("authoritative execution is disabled")
        if not self.safety.reconciliation_enabled:
            raise ExecutionSafetyError("reconciliation is disabled")
        if self.safety.kill_switch:
            raise ExecutionSafetyError("live order submission kill switch is enabled")
        if intent.action.lower() == "sell" and not self.safety.allow_risk_reducing_exits:
            raise ExecutionSafetyError("risk-reducing live exits are not enabled")
        if str(getattr(self.client, "environment", "")).lower() != intent.environment.lower():
            raise ExecutionSafetyError("Kalshi client environment does not match order intent")
        if intent.environment.lower() == "production" and (
            self.safety.production_acknowledgement != PRODUCTION_ACKNOWLEDGEMENT
        ):
            raise ExecutionSafetyError("production execution acknowledgement is missing")
        try:
            await self.repository.assert_position_intent(
                intent.position_id, intent.market_id, intent.side,
                intent.action, intent.quantity,
            )
            if require_health:
                await self.repository.assert_reconciliation_healthy(
                    self.safety.reconciliation_max_age_seconds
                )
        except RuntimeError as exc:
            raise ExecutionSafetyError(str(exc)) from exc

    async def _balance_gateway(self, intent: OrderIntent) -> None:
        try:
            balance = await self.client.get_balance()
        except Exception as exc:
            raise ExecutionSafetyError("balance could not be verified") from exc
        if not isinstance(balance, dict) or isinstance(balance.get("balance"), bool):
            raise ExecutionSafetyError("balance response is malformed")
        try:
            available = Decimal(str(balance["balance"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ExecutionSafetyError("balance response is malformed") from exc
        required = Decimal(str(intent.price)) * 100 * Decimal(str(intent.quantity))
        if intent.action.lower() == "buy" and available < required:
            raise ExecutionSafetyError("insufficient verified balance")

    @staticmethod
    def _validate_intent(intent: OrderIntent) -> None:
        if not intent.market_id.strip() or intent.position_id is None:
            raise ValueError("market_id and position_id are required")
        if intent.side.upper() not in {"YES", "NO"}:
            raise ValueError("side must be YES or NO")
        if intent.action.lower() not in {"buy", "sell"}:
            raise ValueError("action must be buy or sell")
        if intent.order_type.lower() not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        quantity, price = Decimal(str(intent.quantity)), Decimal(str(intent.price))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("quantity must be positive and finite")
        if not price.is_finite() or not Decimal("0.01") <= price <= Decimal("0.99"):
            raise ValueError("price must be between 0.01 and 0.99")
        if intent.environment.lower() not in {"demo", "production"}:
            raise ValueError("environment must be demo or production")

    @staticmethod
    def _order_params(intent: OrderIntent, client_order_id: str) -> dict:
        cents = int((Decimal(str(intent.price)) * 100).quantize(Decimal("1")))
        params = {"ticker": intent.market_id, "client_order_id": client_order_id,
                  "side": intent.side.lower(), "action": intent.action.lower(),
                  "count": intent.quantity, "type_": intent.order_type.lower()}
        params["yes_price" if intent.side.upper() == "YES" else "no_price"] = cents
        return params

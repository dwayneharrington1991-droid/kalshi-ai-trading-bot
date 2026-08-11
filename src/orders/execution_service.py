"""Fail-closed live execution backed by the authoritative order ledger."""

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.orders.models import Order
from src.orders.reconciler import OrderReconciler
from src.orders.readiness import (
    PRODUCTION_ACKNOWLEDGEMENT, ReadinessContext, SubmissionReadinessEvaluator,
    sufficient_order_balance, valid_order_price,
)
from src.orders.repository import OrderRepository
from src.orders.state_machine import OrderState, OrderStateMachine
from src.orders.decision_log import log_order_decision
from src.utils.logging_setup import get_trading_logger
from src.strategies.directional_policy import (
    evaluate_directional_candidate, executable_liquidity, log_directional_evaluation,
)
from src.utils.market_prices import get_market_prices
from src.config.settings import settings

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
    estimated_probability: Optional[float] = None
    model_generated_at: Optional[float] = None
    market_phase: str = "NOT_APPLICABLE"

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
    overnight_canary_enabled: bool = False
    canary_max_total_risk: float = 5.0
    canary_max_market_risk: float = 1.0
    canary_max_positions: int = 5
    canary_max_rejections: int = 3
    canary_max_daily_loss: float = 2.0
    canary_market_data_max_age_seconds: int = 120


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
        try:
            self._validate_intent(intent)
        except (TypeError, ValueError):
            await self._log_refusal(intent, valid_quote=valid_order_price(intent.price))
            raise
        fingerprint = intent.fingerprint()
        existing = await self.repository.get_order_by_fingerprint(fingerprint)
        matching_active = await self.repository.get_matching_active_order(
            intent.market_id, intent.side, intent.action
        )
        if matching_active is not None and (
            existing is None or matching_active["id"] != existing["id"]
        ):
            raise ExecutionSafetyError(
                "matching active or verification-failed order already exists"
            )
        requires_submission = existing is None or existing["state"] == "locally_created"
        await self._preflight_gateway(intent, require_health=requires_submission)
        if requires_submission:
            await self._canary_gateway(intent)
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
        await self._fresh_directional_gateway(intent)
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
            log_order_decision(
                self.logger, market_id=intent.market_id, strategy=intent.strategy,
                side=intent.side, action=intent.action, price=intent.price,
                quantity=intent.quantity, outcome="PENDING",
                reason="exchange identity accepted; authoritative fill pending",
                api_attempted=True, order_id=exchange_order_id,
            )
        except Exception as exc:
            await self._quarantine(
                order_id,
                f"submission outcome unverifiable ({type(exc).__name__})",
            )
            log_order_decision(
                self.logger, market_id=intent.market_id, strategy=intent.strategy,
                side=intent.side, action=intent.action, price=intent.price,
                quantity=intent.quantity, outcome="BLOCKED",
                reason="submission outcome unverifiable", api_attempted=True,
                api_error_category=type(exc).__name__,
            )
            return ExecutionResult(order_id, client_order_id, "verification_failed", submitted=True)
        return await self._verify_and_project(order_id, client_order_id, submitted=True)

    async def _fresh_directional_gateway(self, intent: OrderIntent) -> None:
        """Final GET-only no-chase check immediately before HTTP submission."""
        if intent.strategy not in {"portfolio_optimization", "immediate_portfolio_optimization"}:
            return
        if intent.estimated_probability is None:
            raise ExecutionSafetyError("directional model probability is unavailable")
        if intent.market_phase == "FAST_LIVE":
            if intent.model_generated_at is None or (
                time.time() - intent.model_generated_at
                > settings.trading.fast_live_max_model_age_seconds
            ):
                raise ExecutionSafetyError("FAST_LIVE model probability is stale")
        try:
            market_response = await self.client.get_market(intent.market_id)
            market = market_response.get("market") if isinstance(market_response, dict) else None
            orderbook = await self.client.get_orderbook(intent.market_id, depth=100)
        except Exception as exc:
            raise ExecutionSafetyError("fresh directional quote could not be verified") from exc
        if not isinstance(market, dict):
            raise ExecutionSafetyError("fresh directional market response is malformed")
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(market)
        evaluation = evaluate_directional_candidate(
            market_id=intent.market_id,
            predicted_yes_probability=intent.estimated_probability,
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
            min_probability=settings.trading.min_preferred_probability,
            max_preferred_probability=settings.trading.max_preferred_probability,
            min_edge=settings.trading.min_directional_edge,
            fee_estimate=settings.trading.directional_fee_estimate,
            slippage_estimate=settings.trading.directional_slippage_estimate,
            sports_phase=intent.market_phase,
        )
        log_directional_evaluation(self.logger, evaluation)
        fresh_price = yes_ask if intent.side.upper() == "YES" else no_ask
        if not evaluation.accepted or evaluation.side != intent.side.upper():
            raise ExecutionSafetyError(f"fresh directional edge rejected: {evaluation.reason}")
        if intent.market_phase == "FAST_LIVE" and abs(fresh_price - intent.price) > 1e-9:
            raise ExecutionSafetyError("FAST_LIVE quote moved; entry decision canceled without chasing")
        available = executable_liquidity(orderbook, intent.side, intent.price)
        if available < intent.quantity:
            raise ExecutionSafetyError("insufficient executable liquidity for requested quantity")

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
        try:
            context = ReadinessContext(
                live_mode=self.safety.live_mode,
                authoritative_execution_enabled=self.safety.authoritative_execution_enabled,
                reconciliation_enabled=self.safety.reconciliation_enabled,
                reconciliation_shadow_mode=True,
                kill_switch=self.safety.kill_switch,
                configured_environment=intent.environment,
                client_environment=str(getattr(self.client, "environment", "")),
                production_acknowledgement=self.safety.production_acknowledgement,
                reconciliation_max_age_seconds=self.safety.reconciliation_max_age_seconds,
                allow_risk_reducing_exits=self.safety.allow_risk_reducing_exits,
                action=intent.action,
                position_id=intent.position_id,
                market_id=intent.market_id,
                side=intent.side,
                quantity=intent.quantity,
            )
            evaluator = SubmissionReadinessEvaluator(self.repository)
            report = await evaluator.evaluate(
                context, scope="pre_submission", require_health=require_health
            )
            if report.blocking_checks:
                evaluator.log(report, self.logger)
                raise RuntimeError(report.blocking_checks[0].reason)
        except RuntimeError as exc:
            raise ExecutionSafetyError(str(exc)) from exc

    async def _balance_gateway(self, intent: OrderIntent) -> None:
        try:
            balance = await self.client.get_balance()
        except Exception as exc:
            await self._log_refusal(intent, sufficient_balance=False)
            raise ExecutionSafetyError("balance could not be verified") from exc
        if not isinstance(balance, dict) or isinstance(balance.get("balance"), bool):
            await self._log_refusal(intent, sufficient_balance=False)
            raise ExecutionSafetyError("balance response is malformed")
        try:
            available = Decimal(str(balance["balance"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            await self._log_refusal(intent, sufficient_balance=False)
            raise ExecutionSafetyError("balance response is malformed") from exc
        required = Decimal(str(intent.price)) * 100 * Decimal(str(intent.quantity))
        if not sufficient_order_balance(available, intent.price, intent.quantity, intent.action):
            await self._log_refusal(
                intent, sufficient_balance=False, available_balance=float(available),
                required_balance=float(required),
            )
            raise ExecutionSafetyError("insufficient verified balance")

    async def _canary_gateway(self, intent: OrderIntent) -> None:
        if not self.safety.overnight_canary_enabled:
            return
        snapshot = await self.repository.get_canary_risk_snapshot(
            intent.market_id, self.safety.canary_market_data_max_age_seconds
        )
        order_risk = float(Decimal(str(intent.price)) * Decimal(str(intent.quantity)))
        opening = intent.action.lower() == "buy"
        if opening and order_risk > self.safety.canary_max_market_risk:
            raise ExecutionSafetyError("overnight canary per-market capital limit exceeded")
        if opening and (
            snapshot["total_risk"] + order_risk > self.safety.canary_max_total_risk
        ):
            raise ExecutionSafetyError("overnight canary total capital-at-risk limit exceeded")
        if opening and snapshot["open_positions"] >= self.safety.canary_max_positions:
            raise ExecutionSafetyError("overnight canary open-position limit reached")
        if opening and snapshot["consecutive_rejections"] >= self.safety.canary_max_rejections:
            raise ExecutionSafetyError("overnight canary stopped after consecutive rejected orders")
        if opening and snapshot["daily_pnl"] <= -abs(self.safety.canary_max_daily_loss):
            raise ExecutionSafetyError("overnight canary daily loss limit reached")
        if snapshot["market_data_stale"]:
            raise ExecutionSafetyError("overnight canary market data is stale or unavailable")

    async def _log_refusal(self, intent: OrderIntent, **observations: Any) -> None:
        try:
            evaluator = SubmissionReadinessEvaluator(self.repository)
            report = await evaluator.evaluate(ReadinessContext(
                live_mode=self.safety.live_mode,
                authoritative_execution_enabled=self.safety.authoritative_execution_enabled,
                reconciliation_enabled=self.safety.reconciliation_enabled,
                reconciliation_shadow_mode=True,
                kill_switch=self.safety.kill_switch,
                configured_environment=intent.environment,
                client_environment=str(getattr(self.client, "environment", "")),
                production_acknowledgement=self.safety.production_acknowledgement,
                reconciliation_max_age_seconds=self.safety.reconciliation_max_age_seconds,
                allow_risk_reducing_exits=self.safety.allow_risk_reducing_exits,
                action=intent.action,
                market_id=intent.market_id,
                position_id=intent.position_id,
                side=intent.side,
                quantity=intent.quantity,
                price=intent.price,
                **observations,
            ), scope="pre_submission")
            evaluator.log(report, self.logger)
        except Exception as exc:
            self.logger.error("Submission readiness diagnostic failed")

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
        if not valid_order_price(price):
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

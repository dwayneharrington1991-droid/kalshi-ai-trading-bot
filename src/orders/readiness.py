"""Centralized, secret-safe live submission readiness diagnostics."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from decimal import Decimal, InvalidOperation


PRODUCTION_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_PRODUCTION_ORDER_RISK"
POSITION_INTENT_REASONS = {
    "position does not exist",
    "order intent does not match its position",
    "sell intent exceeds verified open position quantity",
}


def valid_order_price(value: Any) -> bool:
    """Shared Kalshi price predicate; accepts only finite dollar prices 0.01–0.99."""
    try:
        price = Decimal(str(value))
        return price.is_finite() and Decimal("0.01") <= price <= Decimal("0.99")
    except (InvalidOperation, TypeError, ValueError):
        return False


def sufficient_order_balance(available: Any, price: Any, quantity: Any,
                             action: str) -> bool:
    """Shared balance predicate using Kalshi's cent-denominated balance."""
    if action.lower() != "buy":
        return True
    try:
        available_value = Decimal(str(available))
        required = Decimal(str(price)) * 100 * Decimal(str(quantity))
        return available_value.is_finite() and required.is_finite() and available_value >= required
    except (InvalidOperation, TypeError, ValueError):
        return False


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    DISABLED = "DISABLED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: CheckStatus
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    required: bool = False

    def safe_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "metadata": {key: _safe_log_value(value)
                         for key, value in self.metadata.items()},
            "required": self.required,
        }


@dataclass
class ReadinessContext:
    live_mode: bool
    authoritative_execution_enabled: bool
    reconciliation_enabled: bool
    reconciliation_shadow_mode: bool
    kill_switch: bool
    configured_environment: str
    client_environment: Optional[str] = None
    production_acknowledgement: str = ""
    reconciliation_max_age_seconds: int = 30
    allow_risk_reducing_exits: bool = False
    action: Optional[str] = None
    position_id: Optional[int] = None
    market_id: Optional[str] = None
    side: Optional[str] = None
    quantity: Optional[float] = None
    total_markets: Optional[int] = None
    eligible_markets: Optional[int] = None
    opportunity_count: Optional[int] = None
    allocation_count: Optional[int] = None
    allocated_dollars: Optional[float] = None
    cash_reserve_ok: Optional[bool] = None
    cash_reserve_pct: Optional[float] = None
    position_limit_ok: Optional[bool] = None
    current_positions: Optional[int] = None
    max_positions: Optional[int] = None
    existing_position_blocked: Optional[bool] = None
    valid_quote: Optional[bool] = None
    price: Optional[float] = None
    sufficient_balance: Optional[bool] = None
    available_balance: Optional[float] = None
    required_balance: Optional[float] = None


@dataclass(frozen=True)
class ReadinessReport:
    mode: str
    overall: str
    checks: List[ReadinessCheck]
    scope: str = "cycle"

    @property
    def blocking_checks(self) -> List[ReadinessCheck]:
        return [check for check in self.checks if check.required and check.status != CheckStatus.PASS]

    def to_dict(self) -> dict:
        return {
            "event": "trading_submission_readiness",
            "scope": self.scope,
            "mode": self.mode,
            "overall": self.overall,
            "blocking_count": len(self.blocking_checks),
            "checks": [check.safe_dict() for check in self.checks],
        }

    def format_summary(self) -> str:
        lines = ["Trading Submission Readiness", f"Mode: {self.mode}", f"Overall: {self.overall}", ""]
        for check in self.checks:
            metadata = ", ".join(f"{key}={value}" for key, value in check.metadata.items())
            suffix = f" ({metadata})" if metadata else ""
            lines.append(f"{check.status.value:<14} {check.name} — {check.reason}{suffix}")
        if self.mode == "PAPER":
            lines.append(
                f"{CheckStatus.NOT_APPLICABLE.value:<14} Live submission — paper mode is isolated"
            )
        else:
            lines.append(
                f"{'BLOCKED' if self.blocking_checks else 'PASS':<14} Live submission — "
                f"{len(self.blocking_checks)} required gate(s) failed"
            )
        return "\n".join(lines)


class SubmissionReadinessEvaluator:
    """Evaluate the same fail-closed predicates used immediately before submission."""

    def __init__(self, repository: Any = None):
        self.repository = repository

    async def evaluate(self, context: ReadinessContext, *, scope: str = "cycle",
                       require_health: bool = True) -> ReadinessReport:
        checks: List[ReadinessCheck] = []
        live = context.live_mode
        checks.append(self._check("Resolved mode", CheckStatus.PASS, f"resolved to {'live' if live else 'paper'}",
                                  {"mode": "live" if live else "paper"}))
        if scope == "pre_submission":
            checks.append(self._bool("Live submission mode", live, "live mode is enabled",
                                     "live mode is not enabled", required=True))
        if not live:
            checks.extend(self._paper_checks(context))
        else:
            checks.extend(self._configuration_checks(context))
            checks.extend(await self._reconciliation_checks(context, require_health=require_health))
            checks.append(await self._position_intent_check(context, scope))
        checks.extend(self._cycle_checks(context))
        blocking = [item for item in checks if item.required and item.status != CheckStatus.PASS]
        overall = "READY" if live and not blocking else ("PAPER" if not live else "BLOCKED")
        return ReadinessReport("LIVE" if live else "PAPER", overall, checks, scope)

    async def assert_submission_ready(self, context: ReadinessContext, *,
                                      require_health: bool = True) -> ReadinessReport:
        report = await self.evaluate(context, scope="pre_submission", require_health=require_health)
        if report.blocking_checks:
            raise RuntimeError(report.blocking_checks[0].reason)
        return report

    @staticmethod
    def log(report: ReadinessReport, logger: Any) -> None:
        try:
            logger.info(report.format_summary())
        except Exception:
            pass
        try:
            payload = report.to_dict()
            payload.pop("event", None)
            logger.info("trading_submission_readiness", **payload)
        except Exception:
            pass

    def _configuration_checks(self, c: ReadinessContext) -> List[ReadinessCheck]:
        configured_raw = str(c.configured_environment).lower()
        client_raw = str(c.client_environment or "").lower()
        configured = configured_raw if configured_raw in {"demo", "production"} else "unknown"
        client = client_raw if client_raw in {"demo", "production"} else "unknown"
        environment_ok = configured in {"demo", "production"} and client == configured
        acknowledgement_ok = configured != "production" or c.production_acknowledgement == PRODUCTION_ACKNOWLEDGEMENT
        return [
            self._bool("Authoritative execution", c.authoritative_execution_enabled,
                       "enabled", "authoritative execution is disabled", required=True, disabled=True),
            self._bool("Order reconciliation", c.reconciliation_enabled,
                       "enabled", "reconciliation is disabled", required=True, disabled=True),
            self._bool("Reconciliation shadow mode", c.reconciliation_shadow_mode,
                       "enabled", "disabled", required=False, disabled=True),
            self._bool("Kill switch", not c.kill_switch, "off",
                       "live order submission kill switch is enabled", required=True),
            self._bool("Kalshi environment", environment_ok, "client and configured environment match",
                       "Kalshi client environment does not match order intent", required=True,
                       metadata={"configured": configured, "client": client or "unknown"}),
            self._bool("Production acknowledgement", acknowledgement_ok,
                       "present or not required", "production execution acknowledgement is missing", required=True,
                       metadata={"required": configured == "production"}),
            self._bool("Risk-reducing exits", c.action != "sell" or c.allow_risk_reducing_exits,
                       "permitted or not required", "risk-reducing live exits are not enabled",
                       required=c.action == "sell"),
        ]

    async def _position_intent_check(self, c: ReadinessContext, scope: str) -> ReadinessCheck:
        values = (c.position_id, c.market_id, c.side, c.action, c.quantity)
        if scope != "pre_submission" or any(value is None for value in values):
            return self._na("Position intent")
        if self.repository is None:
            return self._check("Position intent", CheckStatus.BLOCKED,
                               "position intent could not be verified", required=True)
        try:
            await self.repository.assert_position_intent(
                c.position_id, c.market_id, c.side, c.action, c.quantity
            )
        except RuntimeError as exc:
            reason = str(exc)
            if reason in POSITION_INTENT_REASONS:
                return self._check("Position intent", CheckStatus.FAIL, reason, required=True)
            return self._check("Position intent", CheckStatus.BLOCKED,
                               "position intent could not be verified", required=True)
        except Exception:
            return self._check("Position intent", CheckStatus.BLOCKED,
                               "position intent could not be verified", required=True)
        return self._check("Position intent", CheckStatus.PASS,
                           "position identity and capacity verified", required=True)

    async def _reconciliation_checks(self, c: ReadinessContext, *,
                                     require_health: bool) -> List[ReadinessCheck]:
        if not c.reconciliation_enabled:
            return [
                self._check("Latest reconciliation health", CheckStatus.BLOCKED,
                            "reconciliation is disabled", required=require_health),
                self._check("Reconciliation freshness", CheckStatus.BLOCKED,
                            "reconciliation is disabled", required=require_health),
                self._check("Critical reconciliation alerts", CheckStatus.BLOCKED,
                            "reconciliation is disabled", required=require_health),
            ]
        if self.repository is None:
            return [self._check(name, CheckStatus.BLOCKED, "reconciliation status unavailable", required=require_health)
                    for name in ("Latest reconciliation health", "Reconciliation freshness",
                                 "Critical reconciliation alerts")]
        try:
            health = await self.repository.get_reconciliation_health(
                c.reconciliation_max_age_seconds
            )
        except Exception:
            return [self._check(
                name, CheckStatus.BLOCKED,
                "reconciliation status unavailable due to database error",
                required=require_health,
            ) for name in ("Latest reconciliation health", "Reconciliation freshness",
                           "Critical reconciliation alerts")]
        return [
            self._bool("Latest reconciliation health", health["healthy_status"],
                       "latest checkpoint completed", health["health_reason"], required=require_health,
                       metadata={"status": health.get("status") or "none"}),
            self._bool("Reconciliation freshness", health["fresh"], "checkpoint is fresh",
                       health["freshness_reason"], required=require_health,
                       metadata={"age_seconds": health.get("age_seconds"),
                                 "max_age_seconds": c.reconciliation_max_age_seconds}),
            self._bool("Critical reconciliation alerts", health["critical_alert_count"] == 0,
                       "no unresolved critical alerts", "unresolved critical reconciliation alert",
                       required=require_health, metadata={"count": health["critical_alert_count"]}),
        ]

    def _cycle_checks(self, c: ReadinessContext) -> List[ReadinessCheck]:
        checks = []
        checks.append(self._optional_count("Markets available", c.total_markets, lambda n: n > 0,
                                           "market(s) available", "no markets available"))
        checks.append(self._optional_count("Eligible markets", c.eligible_markets, lambda n: n > 0,
                                           "market(s) eligible", "no eligible markets"))
        checks.append(self._optional_count("AI opportunities", c.opportunity_count, lambda n: n > 0,
                                           "opportunity(s) passed filtering", "no AI opportunities"))
        if c.allocation_count is None:
            checks.append(self._na("Portfolio allocation"))
        else:
            checks.append(self._bool("Portfolio allocation", c.allocation_count > 0,
                                     "allocations available", "zero allocations after risk constraints",
                                     metadata={"count": c.allocation_count,
                                               "allocated_dollars": round(c.allocated_dollars or 0, 2)},
                                     required=True))
        checks.extend([
            self._optional_bool("Cash reserves", c.cash_reserve_ok, "cash-reserve gate passed",
                                "cash-reserve gate blocked", {"reserve_pct": c.cash_reserve_pct}),
            self._optional_bool("Position limit", c.position_limit_ok, "position-limit gate passed",
                                "position-limit gate blocked", {"current": c.current_positions,
                                                                 "maximum": c.max_positions}),
            self._optional_bool("Existing position", None if c.existing_position_blocked is None else not c.existing_position_blocked,
                                "no duplicate or existing-position blocker", "matching position or order already exists"),
            self._optional_bool("Quote and price", c.valid_quote, "quote and price are valid",
                                "price must be between 0.01 and 0.99", {"price": c.price}),
            self._optional_bool("Sufficient balance", c.sufficient_balance, "verified balance is sufficient",
                                "insufficient verified balance", {"available": c.available_balance,
                                                                      "required": c.required_balance}),
        ])
        return checks

    def _paper_checks(self, c: ReadinessContext) -> List[ReadinessCheck]:
        names = ("Authoritative execution", "Order reconciliation", "Reconciliation shadow mode",
                 "Kill switch", "Kalshi environment", "Production acknowledgement",
                 "Latest reconciliation health", "Reconciliation freshness",
                 "Critical reconciliation alerts", "Position intent")
        return [self._check(name, CheckStatus.NOT_APPLICABLE, "paper mode is isolated from live submission")
                for name in names]

    @staticmethod
    def _check(name, status, reason, metadata=None, required=False):
        return ReadinessCheck(name, status, reason,
                              {k: v for k, v in (metadata or {}).items() if v is not None}, required)

    def _bool(self, name, value, pass_reason, fail_reason, required=False, metadata=None, disabled=False):
        status = CheckStatus.PASS if value else (CheckStatus.DISABLED if disabled else CheckStatus.FAIL)
        return self._check(name, status, pass_reason if value else fail_reason, metadata, required)

    def _optional_bool(self, name, value, pass_reason, fail_reason, metadata=None):
        return self._na(name) if value is None else self._bool(
            name, value, pass_reason, fail_reason, metadata=metadata, required=True
        )

    def _optional_count(self, name, value, predicate, pass_reason, fail_reason):
        return self._na(name) if value is None else self._bool(
            name, predicate(value), pass_reason, fail_reason,
            metadata={"count": value}, required=True
        )

    def _na(self, name):
        return self._check(name, CheckStatus.NOT_APPLICABLE, "not measured at this diagnostic point")


def _safe_log_value(value: Any) -> Any:
    """Constrain structured metadata to stable JSON scalar values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    return f"<{type(value).__name__}>"

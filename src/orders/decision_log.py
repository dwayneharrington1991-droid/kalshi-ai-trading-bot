"""Stable, secret-safe structured logging for every order decision."""

from typing import Any, Optional


def log_order_decision(
    logger: Any, *, market_id: str, strategy: Optional[str], side: str,
    action: str, price: Optional[float], quantity: Optional[float],
    outcome: str, reason: str, risk_limit: Optional[str] = None,
    api_attempted: bool = False, order_id: Optional[str] = None,
    api_error_category: Optional[str] = None,
) -> None:
    """Log bounded metadata only; raw exceptions and responses are forbidden."""
    logger.info(
        "ORDER_DECISION",
        market_id=market_id, strategy=strategy or "unknown", side=side.upper(),
        action=action.lower(), intended_price=price, intended_quantity=quantity,
        outcome=outcome, reason=reason, active_risk_limit=risk_limit,
        exchange_api_attempted=api_attempted, exchange_order_id=order_id,
        api_error_category=api_error_category,
    )

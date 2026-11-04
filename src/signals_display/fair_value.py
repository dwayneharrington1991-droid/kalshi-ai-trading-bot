"""Decimal-safe binary fair-value and executable-price comparison."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from .contract_rules import decimal_value


def fee(price, quantity, multiplier):
    p = decimal_value(price, low=Decimal(0), high=Decimal(1))
    q = decimal_value(quantity, low=Decimal("0.000001"))
    m = decimal_value(multiplier, low=Decimal(0))
    return (Decimal("0.07") * m * q * p * (Decimal(1) - p)).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )


def compare(*, probability, bid, ask, ask_depth, quantity, fee_multiplier,
            price_increment, slippage_ticks=1):
    try:
        p = decimal_value(probability, low=Decimal(0), high=Decimal(1))
        b = decimal_value(bid, low=Decimal(0), high=Decimal(1))
        a = decimal_value(ask, low=b, high=Decimal(1))
        depth = decimal_value(ask_depth, low=Decimal(0))
        qty = decimal_value(quantity, low=Decimal("0.000001"))
        tick = decimal_value(price_increment, low=Decimal("0.00000001"), high=Decimal(1))
        slips = decimal_value(slippage_ticks, low=Decimal(0))
        if qty > depth:
            raise ValueError("requested quantity exceeds displayed executable ask depth")
        entry = a + tick * slips
        if entry >= 1:
            raise ValueError("slippage-adjusted entry is not a valid contract price")
        costs = fee(entry, qty, fee_multiplier)
        gross = p * qty - entry * qty
        net = gross - costs
        total_cost = entry * qty + costs
        return {
            "status": "EXPERIMENTAL", "fair_value": str(p), "executable_bid": str(b),
            "executable_ask": str(a), "spread": str(a - b), "displayed_ask_depth": str(depth),
            "quantity": str(qty), "price_increment": str(tick), "slippage_ticks": str(slips),
            "slippage_adjusted_entry": str(entry), "estimated_fee": str(costs),
            "net_expected_value": str(net),
            "expected_roi": str(net / total_cost) if total_cost else None,
            "actionable": False, "edge_status": "UNVERIFIED",
        }
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "actionable": False}

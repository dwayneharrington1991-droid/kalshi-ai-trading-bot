"""Decimal-safe, fail-closed KXBTC15M contract-rule snapshots.

The Kalshi market and series responses are the runtime source of truth.  This
module deliberately does not infer a strike, equality rule, price increment,
or averaging method from a ticker.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any


OPERATORS = {
    "greater": ">",
    "greater_or_equal": ">=",
    "above": ">",
    "less": "<",
    "less_or_equal": "<=",
    "below": "<",
}


def decimal_value(value: Any, *, low: Decimal | None = None, high: Decimal | None = None):
    if isinstance(value, bool) or value is None:
        raise ValueError("numeric metadata unavailable")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal metadata") from None
    if not result.is_finite() or (low is not None and result < low) or (
        high is not None and result > high
    ):
        raise ValueError("decimal metadata outside supported range")
    return result


@dataclass(frozen=True)
class ContractRuleSnapshot:
    ticker: str
    strike: str
    comparison_operator: str
    equality_wins_yes: bool
    settlement_source: str
    rules_primary: str
    rules_secondary: str
    price_increment: str | None
    opens_at: str
    closes_at: str
    metadata_received_at: str
    rule_fingerprint: str
    rule_status: str
    averaging_method: str | None
    averaging_window_seconds: int | None
    observation_interval_seconds: int | None
    calculation_evidence: str | None

    def as_dict(self):
        return asdict(self)


def _strike(market):
    floor, cap = market.get("floor_strike"), market.get("cap_strike")
    if floor is None and cap is None:
        for key in ("strike_dollars", "strike", "strike_price", "target_price"):
            if market.get(key) is not None:
                return decimal_value(market[key], low=Decimal("0.00000001"))
        raise ValueError("official strike field unavailable")
    values = [decimal_value(x, low=Decimal("0.00000001")) for x in (floor, cap) if x is not None]
    if len(set(values)) != 1:
        raise ValueError("range or contradictory strike is not a binary threshold")
    return values[0]


def _increment(market):
    for key in ("tick_size_dollars", "price_increment", "price_increment_dollars"):
        if market.get(key) is not None:
            return decimal_value(market[key], low=Decimal("0.00000001"), high=Decimal("1"))
    # A string such as ``linear_cent`` is not treated as a numeric increment.
    # Price-range schedules require an explicit single step across the range.
    ranges = market.get("price_ranges")
    if isinstance(ranges, list) and ranges:
        steps = set()
        for row in ranges:
            if not isinstance(row, dict):
                raise ValueError("malformed price range")
            value = row.get("step", row.get("step_dollars"))
            steps.add(decimal_value(value, low=Decimal("0.00000001"), high=Decimal("1")))
        if len(steps) == 1:
            return steps.pop()
    return None


def snapshot(market: dict, series: dict, *, received_at: str):
    """Create an immutable rule snapshot or return a specific fail-closed result."""
    try:
        if market.get("series_ticker", "KXBTC15M") != "KXBTC15M":
            raise ValueError("market is not KXBTC15M")
        ticker = market["ticker"]
        if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
            raise ValueError("invalid KXBTC15M ticker")
        operator = OPERATORS[str(market.get("strike_type", "")).casefold()]
        strike = _strike(market)
        sources = series.get("settlement_sources") or []
        if len(sources) != 1 or not isinstance(sources[0], dict) or not sources[0].get("name"):
            raise ValueError("one explicit settlement source is required")
        primary, secondary = market.get("rules_primary"), market.get("rules_secondary", "")
        if not isinstance(primary, str) or not primary.strip():
            raise ValueError("official primary rules unavailable")
        if not isinstance(secondary, str):
            raise ValueError("official secondary rules malformed")
        opens = market.get("open_time", market.get("opens_at"))
        closes = market.get("close_time", market.get("closes_at"))
        if not opens or not closes:
            raise ValueError("official contract timestamps unavailable")
        spec = market.get("settlement_spec") or {}
        method = spec.get("method")
        window = spec.get("window_seconds")
        interval = spec.get("observation_interval_seconds")
        supported = (
            method == "arithmetic_mean"
            and isinstance(window, int) and not isinstance(window, bool) and window > 0
            and isinstance(interval, int) and not isinstance(interval, bool) and interval > 0
            and window % interval == 0
        )
        calculation_evidence = "STRUCTURED_API_METADATA" if supported else None
        # Some current Kalshi responses describe the calculation only in the
        # authoritative rule text. Support that exact published statement,
        # while rejecting paraphrases and changed wording for review.
        normalized_primary = " ".join(primary.casefold().split())
        source_name = str(sources[0]["name"]).casefold()
        if (
            not supported
            and normalized_primary.count("simple average of the sixty seconds") == 2
            and "brti" in normalized_primary
            and "cf benchmarks" in source_name
        ):
            method, window, interval = "arithmetic_mean", 60, 1
            supported = True
            calculation_evidence = "EXACT_KALSHI_RULE_TEXT_MATCH"
        increment = _increment(market)
        evidence = {
            "ticker": ticker, "strike": str(strike), "strike_type": market.get("strike_type"),
            "rules_primary": primary.strip(), "rules_secondary": secondary.strip(),
            "settlement_sources": sources, "settlement_spec": spec,
            "price_increment": str(increment) if increment is not None else None,
        }
        fingerprint = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ContractRuleSnapshot(
            ticker=ticker, strike=str(strike), comparison_operator=operator,
            equality_wins_yes=operator in {">=", "<="},
            settlement_source=str(sources[0]["name"]).strip(),
            rules_primary=primary.strip(), rules_secondary=secondary.strip(),
            price_increment=evidence["price_increment"], opens_at=str(opens), closes_at=str(closes),
            metadata_received_at=received_at, rule_fingerprint=fingerprint,
            rule_status="STRUCTURED_SETTLEMENT_RULE_VERIFIED" if supported else "SETTLEMENT_CALCULATION_UNVERIFIED",
            averaging_method=method if supported else None,
            averaging_window_seconds=window if supported else None,
            observation_interval_seconds=interval if supported else None,
            calculation_evidence=calculation_evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return {"rule_status": "INVALID_OR_INCOMPLETE_METADATA", "reason": str(exc)}


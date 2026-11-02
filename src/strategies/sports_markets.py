"""Structured sports derivative classification and event-level exposure controls."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


MARKET_TYPES = {"WINNER", "SPREAD", "GAME_TOTAL", "TEAM_TOTAL", "PROP", "OTHER"}


def classify_sports_market_type(market: dict[str, Any]) -> str:
    """Classify a sports contract without excluding unknown/future derivatives."""
    structured = str(
        market.get("market_type") or market.get("product_type") or market.get("type") or ""
    ).upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "MONEYLINE": "WINNER", "MATCH_WINNER": "WINNER", "GAME_WINNER": "WINNER",
        "HANDICAP": "SPREAD", "TOTAL": "GAME_TOTAL", "OVER_UNDER": "GAME_TOTAL",
        "TEAMTOTAL": "TEAM_TOTAL", "PLAYER_PROP": "PROP", "TEAM_PROP": "PROP",
    }
    if structured in MARKET_TYPES:
        return structured
    if structured in aliases:
        return aliases[structured]

    text = " ".join(str(market.get(key, "")) for key in ("ticker", "title", "subtitle", "yes_sub_title")).casefold()
    if "team total" in text:
        return "TEAM_TOTAL"
    if any(token in text for token in ("spread", "handicap", "margin of victory", "win by")):
        return "SPREAD"
    if any(token in text for token in ("total points", "total runs", "total goals", "over/under", "game total")):
        return "GAME_TOTAL"
    if any(token in text for token in ("player", "touchdowns", "strikeouts", "rebounds", "assists", "prop")):
        return "PROP"
    if any(token in text for token in (" winner", "to win", "will win", "moneyline")):
        return "WINNER"
    return "OTHER"


def event_correlation_group(market: dict[str, Any]) -> str:
    """Return the authoritative event identity, failing to a conservative ticker group."""
    for key in ("event_ticker", "series_ticker", "_event_ticker"):
        value = market.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    ticker = str(market.get("ticker", "")).upper()
    # Kalshi child contracts append a final outcome/strike component.
    return re.sub(r"-[^-]+$", "", ticker) or ticker


@dataclass(frozen=True)
class CorrelationDecision:
    accepted: bool
    existing_exposure: float
    reason: str


def admit_event_exposure(
    *, correlation_group: str, proposed_risk: float,
    existing: dict[str, float], selected: dict[str, float], max_event_risk: float,
) -> CorrelationDecision:
    """Apply a conservative event-level cap across winner and derivative markets."""
    current = max(0.0, float(existing.get(correlation_group, 0.0))) + max(
        0.0, float(selected.get(correlation_group, 0.0))
    )
    if current + proposed_risk > max_event_risk + 1e-9:
        return CorrelationDecision(False, current, "correlated event exposure cap exceeded")
    return CorrelationDecision(True, current, "correlated event exposure within cap")


def deduplicate_and_cap_event_exposure(
    opportunities: Iterable[Any], *, max_event_risk: float,
    existing: dict[str, float] | None = None,
) -> list[Any]:
    """Rank independently, deduplicate contracts, then cap aggregate game exposure."""
    selected: list[Any] = []
    seen_markets: set[str] = set()
    selected_exposure: dict[str, float] = {}
    existing = existing or {}
    for opportunity in sorted(opportunities, key=lambda item: item.ranking_score, reverse=True):
        if opportunity.market_id in seen_markets:
            continue
        group = opportunity.correlation_group or opportunity.market_id
        proposed = min(max_event_risk, max(0.01, float(getattr(opportunity, "proposed_risk", max_event_risk))))
        decision = admit_event_exposure(
            correlation_group=group, proposed_risk=proposed, existing=existing,
            selected=selected_exposure, max_event_risk=max_event_risk,
        )
        opportunity.existing_correlated_exposure = decision.existing_exposure
        opportunity.correlation_reason = decision.reason
        if not decision.accepted:
            continue
        seen_markets.add(opportunity.market_id)
        selected_exposure[group] = selected_exposure.get(group, 0.0) + proposed
        selected.append(opportunity)
    return selected

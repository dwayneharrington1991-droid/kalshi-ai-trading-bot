"""Fail-closed deterministic coordination of existing signals-only analytics."""
from __future__ import annotations

from .risk_controls import signal_kill_switch


def _market_scout(state):
    health = state.get("market_health", {})
    market = state.get("market") or {}
    quotes = market.get("quotes", {})
    spreads = {}
    for side, quote in quotes.items():
        bid, ask = quote.get("bid"), quote.get("ask")
        spreads[side] = ask - bid if bid is not None and ask is not None else None
    return {
        "status": "READY" if not state.get("stale", True) else "BLOCKED",
        "contract": market.get("ticker"),
        "contract_quotes_fresh": bool(health.get("contract_quotes_fresh")),
        "reference_fresh": bool(health.get("reference_fresh")),
        "spreads": spreads,
        "order_flow": (state.get("model", {}).get("scanner", {}) or {}).get("order_flow"),
        "advanced_feature_status": state.get("advanced_intelligence", {}).get("status"),
    }


def _quant_analyst(state):
    engine = state.get("prediction_engine", {})
    initial = engine.get("initial") or {}
    edge = state.get("edge_scanner", {})
    return {
        "status": "EXPERIMENTAL" if engine.get("up") is not None else "UNAVAILABLE",
        "opening": {
            "status": initial.get("status", "UNAVAILABLE"),
            "up_probability": initial.get("up"),
            "immutable": bool(initial.get("generated_at")),
        },
        "settlement": {
            "status": engine.get("status", "UNAVAILABLE"),
            "up_probability": engine.get("up"),
        },
        "edge_validation": edge.get("validation", {}),
        "edge_status": edge.get("label", "NO VERIFIED EDGE — WAIT"),
        "settlement_intelligence": state.get("settlement_intelligence", {}),
    }


def _risk_guardian(state, scout, quant):
    blockers = []
    if state.get("stale", True):
        blockers.append("STALE_OR_INCOMPLETE_DATA")
    if not scout["contract_quotes_fresh"]:
        blockers.append("CONTRACT_QUOTES_NOT_FRESH")
    if not scout["reference_fresh"]:
        blockers.append("REFERENCE_PRICE_NOT_FRESH")
    if any(value is not None and value > 0.03 for value in scout["spreads"].values()):
        blockers.append("EXECUTABLE_SPREAD_ABOVE_3_CENTS")
    validation = quant.get("edge_validation") or {}
    if not validation.get("verified", False):
        blockers.append("PROFITABLE_EDGE_UNVERIFIED")
    rule_status = (
        quant.get("settlement_intelligence", {}).get("rule_snapshot", {}).get("rule_status")
    )
    if rule_status != "STRUCTURED_SETTLEMENT_RULE_VERIFIED":
        blockers.append("OFFICIAL_SETTLEMENT_CALCULATION_UNVERIFIED")
    account = state.get("account", {})
    if account.get("status") == "ACCOUNT_STALE":
        blockers.append("ACCOUNT_DATA_STALE")
    opening_up = quant["opening"].get("up_probability")
    live_up = quant["settlement"].get("up_probability")
    if opening_up is not None and live_up is not None and (opening_up - 0.5) * (live_up - 0.5) < 0:
        blockers.append("OPENING_AND_LIVE_DIRECTION_CONFLICT")
    kill_switch = signal_kill_switch(state)
    blockers.extend(reason for reason in kill_switch["reasons"] if reason not in blockers)
    return {
        "status": "BLOCKED" if blockers else "MANUAL_REVIEW_ONLY",
        "blockers": blockers,
        "stale_kill_switch": state.get("stale", True),
        "signal_kill_switch": kill_switch,
        "automatic_execution_allowed": False,
    }


def coordinate(state):
    """Summarize independent components; never create an execution instruction."""
    scout = _market_scout(state)
    quant = _quant_analyst(state)
    guardian = _risk_guardian(state, scout, quant)
    reason = (
        "; ".join(guardian["blockers"])
        if guardian["blockers"]
        else "All configured data gates passed; manual review is still required."
    )
    return {
        "recommendation": "WAIT" if guardian["blockers"] else "MANUAL REVIEW",
        "reason": reason,
        "components": {
            "market_scout": scout,
            "quant_analyst": quant,
            "risk_guardian": guardian,
            "decision_coordinator": {
                "status": "FAIL_CLOSED" if guardian["blockers"] else "MANUAL_REVIEW_ONLY",
                "opening_and_settlement_kept_separate": True,
            },
        },
        "signals_only": True,
        "manual_only": True,
        "execution_capability": False,
    }

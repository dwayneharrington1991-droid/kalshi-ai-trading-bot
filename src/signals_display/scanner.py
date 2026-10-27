"""Past-only scanner context. Missing proprietary inputs stay unavailable."""
from .model import timestamp


def context(payload, market, info, now):
    bars = info.get("candidate_features", {}).get("closed_candles", [])
    changes = {}
    for n in (1, 3, 5):
        changes[str(n) + "m"] = (
            bars[-1]["close"] / bars[-n - 1]["close"] - 1 if len(bars) > n else None
        )
    signs = [1 if v > 0 else -1 if v < 0 else 0 for v in changes.values() if v is not None]
    aligned = len(signs) == 3 and abs(sum(signs)) == 3
    structure = {"up": False, "down": False}
    structural_reason = "Unavailable: need 25 contiguous closed one-minute bars"
    if len(bars) >= 25:
        previous, recent = bars[-25:-5], bars[-5:]
        support = min(b["low"] for b in previous)
        resistance = max(b["high"] for b in previous)
        structure["up"] = (
            min(b["low"] for b in recent) <= support
            and recent[-1]["close"] > support
            and recent[-1]["close"] > recent[0]["open"]
        )
        structure["down"] = (
            max(b["high"] for b in recent) >= resistance
            and recent[-1]["close"] < resistance
            and recent[-1]["close"] < recent[0]["open"]
        )
        structural_reason = "Experimental 5m sweep/reclaim of prior 20m high/low; momentum is closed 1m/3m/5m return sign. Fixed candidate filter, not validated predictive evidence."
    spreads = {
        side: q["ask"] - q["bid"]
        for side, q in (market or {}).get("quotes", {}).items()
        if q.get("ask") is not None and q.get("bid") is not None
    }
    releases = payload.get("coverage", {}).get("Federal Reserve public releases", {})
    recent = []
    if releases.get("status") == "CONNECTED":
        for item in releases.get("data", {}).get("items", []):
            try:
                if 0 <= (now - timestamp(item["published_at"])).total_seconds() <= 900:
                    recent.append(item["published_at"])
            except (ValueError, KeyError, TypeError):
                pass
    return {
        "official_60s_brti_twap": None,
        "official_twap_reason": "Unavailable: no licensed official BRTI tick stream. A proxy or OHLC4 is not official TWAP. No protected-average arbitrage claim is supported.",
        "timeframe_returns": changes,
        "same_direction_1m_3m_5m": aligned,
        "confluence_probability": None,
        "structural_candidate": structure,
        "structural_reason": structural_reason,
        "confluence_note": "Past closed-bar returns only; alignment is not calibrated confidence. Structural sweep/reclaim filter is experimental; KDJ/exhaustion confidence is unavailable.",
        "liquidation_clusters": None,
        "liquidation_reason": "Unavailable: no reliable licensed liquidation-cluster feed; not inferred from open interest.",
        "order_flow": payload.get("coverage", {})
        .get("Kraken public order flow", {})
        .get("data", {"status": "unavailable"}),
        "aggressive_trade_flow_ratio": None,
        "flow_reason": "See per-venue CVD diagnostics when live; resting depth is not aggressive executed trade flow. No pooled spot/perpetual ratio.",
        "spreads": spreads,
        "high_slippage_risk": any(v > 0.030000001 for v in spreads.values()),
        "macro_recent_release_timestamps": recent,
        "macro_blackout": bool(recent),
        "macro_calendar_status": "Incomplete: public Fed releases support post-release muting only; no verified forward CPI/FOMC/SEC event calendar. Pre-event coverage unavailable.",
        "edge_status": "edge unverified",
    }


def qualify(state, scan):
    reasons = []
    if scan["high_slippage_risk"]:
        reasons.append("HIGH_SLIPPAGE_RISK: executable spread exceeds 3 cents")
    if scan["macro_blackout"]:
        reasons.append("MACRO_BLACKOUT: public Federal Reserve release within 15 minutes")
    if state.get("scalp") != "WAIT":
        side = "up" if "UP" in state.get("scalp", "") else "down"
        changes = scan["timeframe_returns"]
        sign = 1 if side == "up" else -1
        if (
            not all(v is not None and sign * v > 0 for v in changes.values())
            or not scan["structural_candidate"][side]
        ):
            reasons.append(
                "CONFLUENCE_NOT_MET: closed 1m/3m/5m momentum and structural sweep/reclaim do not support this scalp"
            )
    if reasons:
        state["scalp"] = "WAIT"
        state["scalp_explanation"] = "; ".join(reasons)
    comparison = state.get("market_comparison", {})
    delta = comparison.get("model_minus_market")
    scan["experimental_probability_divergence"] = (
        delta if delta is not None and abs(delta) > 0.10 else None
    )
    scan[
        "divergence_note"
    ] = "Model/market difference is not proof of mispricing or profitable edge; executable prices and all costs are assessed separately."

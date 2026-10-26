"""Separate model lean from cost-aware experimental qualification."""
from .model import fee, number


def assess_edges(market, result, info, now):
    execution = info.get("execution_assumptions", {})
    slip = execution.get("slippage_per_leg", 0.01)
    delay = execution.get("manual_delay_seconds", 3)
    settlement = {}
    for side, p in (("up", result.settlement_up), ("down", result.settlement_down)):
        reason = "WAIT: settlement probability or executable cost inputs unavailable"
        edge = None
        try:
            if p is None:
                raise ValueError("probability unavailable")
            if market["fee_type"] not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("unknown entry fee")
            q = market["quotes"][side]
            if q["ask_size"] < 1:
                raise ValueError("less than one contract offered")
            cap = number(q["ask"] + slip, 0.000001, 0.999999)
            multiplier = number(market["fee_multiplier"], 0, 100)
            entry_fee = fee(cap, multiplier)
            edge = p - cap - entry_fee
            reason = "WAIT: edge unverified; Coinbase/official-index basis and model calibration are unmeasured"
            if edge <= 0:
                reason = "WAIT: non-positive modeled net settlement edge; edge unverified"
            settlement[side] = {
                "estimated_probability": p,
                "executable_ask": q["ask"],
                "entry_cap": cap,
                "entry_fee": entry_fee,
                "slippage_budget": slip,
                "manual_delay_seconds": delay,
                "net_edge_dollars_per_contract": edge,
                "formula": "independent model P(UP/DOWN) - (ask + slippage allowance) - entry fee; hold to $1/$0 settlement, assumed no redemption fee",
                "qualification": "WAIT",
                "reason": reason,
                "edge_status": "edge unverified",
            }
        except (KeyError, ValueError, TypeError):
            settlement[side] = {
                "net_edge_dollars_per_contract": None,
                "qualification": "WAIT",
                "reason": reason,
                "edge_status": "edge unverified",
            }
    scalp = info.get("scalp_edges", {})
    candidates = []
    for side, details in scalp.items():
        p = details.get("target_probability_given_entry")
        ev = details["net_expected_profit_per_opportunity"]
        details["edge_status"] = "edge unverified"
        details["qualification"] = "WAIT"
        details[
            "reason"
        ] = "WAIT: non-positive modeled net EV after fees, spread, slippage and no-entry outcomes; edge unverified"
        if ev > 0:
            details[
                "reason"
            ] = "WAIT: positive modeled EV alone is insufficient; require target probability >=60%, positive MC lower EV bound and >=200 entered paths; edge unverified"
        if (
            ev > 0
            and details["net_ev_sampling_interval"][0] > 0
            and p is not None
            and p >= 0.6
            and details["entered_paths"] >= 200
        ):
            candidates.append((side, ev))
    candidates.sort(key=lambda pair: pair[1], reverse=True)
    call = "WAIT"
    if candidates and (len(candidates) == 1 or candidates[0][1] > candidates[1][1]):
        side = candidates[0][0]
        call = "SCALP " + side.upper()
        scalp[side]["qualification"] = "EXPERIMENTAL_CANDIDATE"
        scalp[side][
            "reason"
        ] = "Positive cost-adjusted model EV and fixed quality thresholds only; edge unverified, not demonstrated profitability"
    return {
        "settlement": settlement,
        "scalp": scalp,
        "scalp_call": call,
        "edge_status": "edge unverified",
        "profitable_edge_demonstrated": False,
        "note": "Numerical Monte Carlo uncertainty excludes model error. No chronological real-data out-of-sample profitable edge has been demonstrated.",
    }

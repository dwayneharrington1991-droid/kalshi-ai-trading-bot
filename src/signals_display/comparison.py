"""Executable-price settlement comparisons; never a validated trade signal."""
import math

from .model import fee, number


def model_market(state, slippage=0.01, manual_delay=3):
    engine = state.get("prediction_engine") or {}
    market = state.get("market") or {}
    ready = engine.get("status") == "EXPERIMENTAL" and not state.get("stale", True)
    sensitivity = state.get("model", {}).get("settlement_probability_sensitivity")
    result = {
        "status": "EDGE UNVERIFIED",
        "validated": False,
        "actionable": False,
        "ticker": market.get("ticker"),
        "sides": {},
        "timestamp": state.get("now"),
        "uncertainty": {
            "calibration": "Experimental, uncalibrated model estimate",
            "probability_sensitivity_up": sensitivity,
            "limitation": "Sensitivity to opening basis is not a calibrated confidence interval; future proxy/index basis error is unmeasured",
        },
        "reason": "No out-of-sample profitable edge demonstrated. Positive theoretical EV is not permission to buy.",
        "formula": "P(winning settlement) - (executable ask + slippage allowance) - estimated entry fee; $1 winning payout, $0 losing payout, assumed no settlement redemption fee",
        "manual_delay_seconds": manual_delay,
        "manual_delay_note": "Price may move before manual entry; this is a quote-time comparison, not a fill simulation or guaranteed cap",
    }
    for side in ("up", "down"):
        quote = market.get("quotes", {}).get(side, {})
        p = engine.get(side) if ready else None
        item = {
            "model_probability": p,
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "bid_size": quote.get("bid_size"),
            "ask_size": quote.get("ask_size"),
            "spread": None,
            "entry_fee": None,
            "slippage": slippage,
            "net_ev": None,
            "sensitivity_net_ev": None,
            "executable": False,
            "reason": "Supported probability, executable depth and verified costs required",
            "quote_received_at": market.get("observed_at"),
            "quote_event_time": market.get("quote_event_time"),
        }
        try:
            bid = number(quote["bid"], 0, 1)
            ask = number(quote["ask"], bid, 1)
            item["spread"] = ask - bid
            if state.get("stale", True):
                raise ValueError("Fresh compatible quote/reference inputs required")
            if market.get("book_diagnostic") or number(quote["ask_size"], 0, 1e15) < 1:
                raise ValueError("One-contract executable ask depth unavailable")
            if market.get("fee_type") not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("Applicable fee schedule unverified")
            cap = number(ask + number(slippage, 0, 0.1), 0.000001, 0.999999)
            entry_fee = fee(cap, number(market["fee_multiplier"], 0, 100))
            item.update(entry_fee=entry_fee, executable=True, entry_cap=cap)
            if not ready or p is None:
                raise ValueError(
                    "Fresh compatible model probability required; quoted costs are estimates"
                )
            number(p, 0, 1)
            item.update(
                entry_fee=entry_fee,
                net_ev=p - cap - entry_fee,
                executable=True,
                entry_cap=cap,
                reason="Experimental quote-time estimate only; EDGE UNVERIFIED",
            )
            if sensitivity and len(sensitivity) == 2 and all(math.isfinite(x) for x in sensitivity):
                values = sensitivity if side == "up" else [1 - sensitivity[1], 1 - sensitivity[0]]
                item["sensitivity_net_ev"] = [v - cap - entry_fee for v in values]
        except (ValueError, KeyError, TypeError) as exc:
            item["reason"] = str(exc)
        result["sides"][side] = item
    return result

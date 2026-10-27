"""Read-only executable-price edge estimates and forward evaluation.

This module cannot execute anything. Positive expected value remains experimental
unless a separately reviewed validation artifact explicitly proves otherwise.
"""
from __future__ import annotations

from datetime import datetime
import json
import math

from .model import number, timestamp
from .scores import score


PROTOCOL = "executable-settlement-edge-v1"
FEE_TYPES = {"quadratic", "quadratic_with_maker_fees"}


def quantity_fee(price, quantity, multiplier):
    """Estimated taker fee for one order, rounded up to the nearest cent."""
    price = number(price, 0, 1)
    quantity = number(quantity, 0.000001, 1e12)
    multiplier = number(multiplier, 0, 100)
    return math.ceil(0.07 * multiplier * quantity * price * (1 - price) * 100 - 1e-12) / 100


def _validation(state):
    learning = state.get("model", {}).get("learning_status", {})
    report = learning.get("heldout_report") or {}
    verified = bool(
        report.get("profitable_edge_demonstrated") is True
        and report.get("protocol") == PROTOCOL
        and report.get("chronological_out_of_sample") is True
        and report.get("transaction_costs_included") is True
        and report.get("selection_bias_controlled") is True
    )
    return {
        "verified": verified,
        "status": "VERIFIED_OUT_OF_SAMPLE" if verified else "EXPERIMENTAL_UNVERIFIED",
        "model_id": learning.get("model_id") or state.get("model", {}).get("version"),
        "reason": (
            "Reviewed chronological out-of-sample edge protocol passed"
            if verified
            else "No reviewed chronological out-of-sample evidence demonstrates predictive advantage after costs and selection bias"
        ),
    }


def calculate(state, *, slippage=0.01, manual_delay=3, account=None, now=None):
    market = state.get("market") or {}
    engine = state.get("prediction_engine") or {}
    now = now or timestamp(state.get("now"))
    validation = _validation(state)
    fresh = (
        not state.get("stale", True)
        and engine.get("status") == "EXPERIMENTAL"
        and market.get("ticker") == engine.get("ticker")
        and market.get("observed_at") is not None
    )
    quote_age = None
    try:
        quote_age = (now - timestamp(market["observed_at"])).total_seconds()
        fresh = fresh and 0 <= quote_age <= 5
    except (KeyError, TypeError, ValueError, AttributeError):
        fresh = False
    result = {
        "protocol": PROTOCOL,
        "ticker": market.get("ticker"),
        "timestamp": now.isoformat(),
        "model_timestamp": engine.get("updated_at"),
        "quote_received_at": market.get("observed_at"),
        "quote_event_time": market.get("quote_event_time"),
        "quote_age_seconds": quote_age,
        "remaining_seconds": state.get("remaining_seconds"),
        "data_status": "FRESH" if fresh else "STALE_OR_INCOMPLETE",
        "validation": validation,
        "label": "NO VERIFIED EDGE — WAIT",
        "actionable": False,
        "sides": {},
        "positions": [],
        "fee_basis": "Kalshi quadratic taker-fee formula using contract fee_multiplier; total order fee rounded up to cents. No settlement redemption fee assumed.",
        "slippage_basis": "Configured quote-time allowance per contract; top-level depth only. It is not a guaranteed execution price.",
        "manual_delay_seconds": manual_delay,
        "reason": validation["reason"],
    }
    for side in ("up", "down"):
        probability = engine.get(side) if fresh else None
        quote = market.get("quotes", {}).get(side, {})
        item = {
            "side": side,
            "model_probability": probability,
            "market_purchase_price": None,
            "market_implied_probability": None,
            "probability_edge": None,
            "entry_price_with_slippage": None,
            "estimated_fee_per_contract": None,
            "break_even_probability": None,
            "net_expected_profit_per_contract": None,
            "expected_roi": None,
            "maximum_profit_per_contract": None,
            "maximum_loss_per_contract": None,
            "available_liquidity": None,
            "maximum_executable_quantity_at_displayed_price": None,
            "spread": None,
            "executable": False,
            "status": "UNAVAILABLE",
            "reason": "Fresh compatible probability, executable ask/depth and verified fee metadata required",
        }
        try:
            if not fresh:
                raise ValueError("Fresh contract, reference and model inputs required")
            if market.get("book_diagnostic"):
                raise ValueError(str(market["book_diagnostic"]))
            if market.get("fee_type") not in FEE_TYPES:
                raise ValueError("Applicable Kalshi fee schedule is unavailable or unsupported")
            bid = number(quote["bid"], 0, 1)
            ask = number(quote["ask"], bid, 1)
            ask_size = number(quote["ask_size"], 0, 1e12)
            number(probability, 0, 1)
            if ask_size < 1:
                raise ValueError("No one-contract quantity available at the displayed ask")
            slip = number(slippage, 0, 0.1)
            entry = number(ask + slip, 0.000001, 0.999999)
            fee = quantity_fee(entry, 1, market["fee_multiplier"])
            cost = entry + fee
            ev = probability - cost
            item.update(
                market_purchase_price=ask,
                market_implied_probability=ask,
                probability_edge=probability - ask,
                entry_price_with_slippage=entry,
                estimated_fee_per_contract=fee,
                break_even_probability=cost,
                net_expected_profit_per_contract=ev,
                expected_roi=ev / cost if cost else None,
                maximum_profit_per_contract=1 - cost,
                maximum_loss_per_contract=cost,
                available_liquidity=ask_size,
                maximum_executable_quantity_at_displayed_price=ask_size,
                spread=ask - bid,
                executable=True,
                status=(
                    "VERIFIED_POSITIVE_EDGE"
                    if ev > 0 and validation["verified"]
                    else "EXPERIMENTAL_POSITIVE_EV"
                    if ev > 0
                    else "NO_POSITIVE_EV"
                ),
                reason=(
                    "Positive estimate under current executable price and costs; validation gate passed"
                    if ev > 0 and validation["verified"]
                    else "Experimental positive EV estimate; predictive advantage after costs is unverified"
                    if ev > 0
                    else "Model probability does not exceed executable break-even probability"
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            item["reason"] = str(exc)
        result["sides"][side] = item
    verified = [x for x in result["sides"].values() if x["status"] == "VERIFIED_POSITIVE_EDGE"]
    if verified:
        best = max(verified, key=lambda x: x["net_expected_profit_per_contract"])
        result["label"] = "POSITIVE EDGE — " + best["side"].upper()
        result["actionable"] = True
        result["reason"] = best["reason"]
    elif any(x["status"] == "EXPERIMENTAL_POSITIVE_EV" for x in result["sides"].values()):
        result["reason"] = "Positive quote-time model EV exists, but edge remains experimental and unverified"
    result["positions"] = _positions(account or {}, result)
    return result


def _positions(account, scanner):
    positions = []
    sides = scanner.get("sides", {})
    actual_realized = sum(
        row.get("realized_pnl", 0)
        for row in account.get("completed_trades", [])
        if isinstance(row.get("realized_pnl"), (int, float))
    )
    for card in account.get("live_trade_cards", []):
        side = card.get("side")
        edge = sides.get(side, {}) if card.get("contract") == scanner.get("ticker") else {}
        quantity = card.get("filled_quantity")
        probability = edge.get("model_probability")
        hold_value = quantity * probability if all(
            isinstance(v, (int, float)) for v in (quantity, probability)
        ) else None
        exit_value = None
        if all(isinstance(card.get(k), (int, float)) for k in ("total_entry_cost", "estimated_exit_net_pnl")):
            exit_value = card["total_entry_cost"] + card["estimated_exit_net_pnl"]
        opportunity = hold_value - exit_value if hold_value is not None and exit_value is not None else None
        positions.append({
            "holding_id": card.get("holding_id"),
            "ticker": card.get("contract"),
            "side": side,
            "confirmed_quantity": quantity,
            "actual_entry_cost": card.get("total_entry_cost"),
            "executable_exit_bid": card.get("current_executable_exit_price"),
            "estimated_exit_now_value_after_fees": exit_value,
            "estimated_hold_value": hold_value,
            "hold_minus_exit_opportunity_value": opportunity,
            "guidance": "WAIT" if card.get("values_stale") else "HOLD" if opportunity is None or opportunity >= 0 else "CONSIDER EXIT",
            "reason": "Forward model value versus executable exit value; experimental and not a guaranteed fill",
        })
    return {
        "open": positions,
        "actual_completed_trade_realized_pnl": actual_realized,
        "actual_completed_trade_count": len(account.get("completed_trades", [])),
        "actual_results_note": "Account-confirmed completed trades only; kept separate from counterfactual edge evaluation",
    }


def create_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS edge_observations (
          mode TEXT NOT NULL,ticker TEXT NOT NULL,side TEXT NOT NULL,
          prediction_at TEXT NOT NULL,recorded_at TEXT NOT NULL,closes_at TEXT NOT NULL,
          probability REAL NOT NULL,ask REAL NOT NULL,entry_price REAL NOT NULL,
          fee REAL NOT NULL,probability_edge REAL NOT NULL,net_ev REAL NOT NULL,
          volatility_regime TEXT,time_band TEXT,validation_status TEXT NOT NULL,
          PRIMARY KEY(mode,ticker,side,prediction_at));
        CREATE INDEX IF NOT EXISTS edge_observations_close ON edge_observations(mode,closes_at);
    """)


def record(db, state, recorded_at):
    scanner, market = state.get("edge_scanner", {}), state.get("market") or {}
    if state.get("stale") or not market or scanner.get("ticker") != market.get("ticker"):
        return
    prediction_at = scanner.get("model_timestamp") or scanner.get("timestamp")
    try:
        if timestamp(prediction_at) > recorded_at or timestamp(prediction_at) >= timestamp(market["closes_at"]):
            return
    except (TypeError, ValueError, KeyError, AttributeError):
        return
    remaining = (timestamp(market["closes_at"]) - timestamp(prediction_at)).total_seconds()
    band = ">10m" if remaining > 600 else "5-10m" if remaining > 300 else "2-5m" if remaining > 120 else "<=2m"
    regime = state.get("model", {}).get("analysis_24h", {}).get("regime", "unavailable")
    for side, item in scanner.get("sides", {}).items():
        if not item.get("executable") or item.get("model_probability") is None:
            continue
        db.execute(
            "INSERT OR IGNORE INTO edge_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (state["mode"], market["ticker"], side, prediction_at, recorded_at.isoformat(),
             market["closes_at"], item["model_probability"], item["market_purchase_price"],
             item["entry_price_with_slippage"], item["estimated_fee_per_contract"],
             item["probability_edge"], item["net_expected_profit_per_contract"], regime, band,
             scanner["validation"]["status"]),
        )


def performance(db):
    rows = [dict(row) for row in db.execute("""
      SELECT e.*,o.outcome,o.observed_at FROM edge_observations e
      JOIN forecast_outcomes o ON o.ticker=e.ticker
       AND o.origin=CASE WHEN e.mode='PUBLIC MARKET DATA' THEN 'official' ELSE 'replay' END
      WHERE julianday(e.prediction_at)<julianday(e.closes_at)
        AND julianday(e.recorded_at)<julianday(e.closes_at)
        AND julianday(e.closes_at)<=julianday(o.observed_at)
      ORDER BY e.prediction_at,e.ticker,e.side
    """)]

    def summarize(group):
        if not group:
            return {"samples": 0, "average_predicted_edge": None, "counterfactual_net_pnl": None,
                    "expected_net_value_total": None, "expected_minus_realized": None,
                    "brier": None, "log_loss": None, "expected_calibration_error": None}
        probabilities = [r["probability"] for r in group]
        outcomes = [r["outcome"] if r["side"] == "up" else 1 - r["outcome"] for r in group]
        metrics = score(probabilities, outcomes)
        realized = [y - r["entry_price"] - r["fee"] for r, y in zip(group, outcomes)]
        expected = [r["net_ev"] for r in group]
        return {**metrics, "average_predicted_edge": sum(r["probability_edge"] for r in group) / len(group),
                "average_net_ev": sum(expected) / len(group), "expected_net_value_total": sum(expected),
                "counterfactual_net_pnl": sum(realized),
                "expected_minus_realized": sum(expected) - sum(realized),
                "scope": "One-contract counterfactual purchases at recorded executable ask plus slippage/fee; never actual fills"}

    bucket = lambda r: f"{int(max(r['probability'],1-r['probability'])*10)/10:.1f}"
    grouped = lambda key: {name: summarize([r for r in rows if key(r) == name]) for name in sorted({key(r) for r in rows})}
    return {
        "status": "READY" if rows else "INSUFFICIENT_SETTLED_EDGE_OBSERVATIONS",
        "all": summarize(rows),
        "by_probability_bucket": grouped(bucket),
        "by_volatility_regime": grouped(lambda r: r["volatility_regime"]),
        "by_time_remaining": grouped(lambda r: r["time_band"]),
        "selection_bias_note": "Repeated intra-contract observations are dependent and opportunity selection can bias results. Metrics are descriptive; contract-level validation and untouched chronological holdouts are required before verification.",
        "profitable_edge_demonstrated": False,
        "actual_account_results_included": False,
    }

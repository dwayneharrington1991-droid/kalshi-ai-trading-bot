from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from src.signals_display.strategy_paper import (
    HOLD_PROTOCOL,
    SCALP_PROTOCOL,
    create_schema,
    evaluate,
    performance,
    record_and_resolve,
)


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
TICKER = "KXBTC15M-26NOV051215-15"


def state():
    return {
        "now": NOW.isoformat(), "mode": "PUBLIC MARKET DATA", "stale": False,
        "remaining_seconds": 600,
        "market": {
            "ticker": TICKER, "closes_at": (NOW + timedelta(minutes=10)).isoformat(),
            "fee_multiplier": 1,
            "quotes": {"up": {"bid": .60, "ask": .61, "bid_size": 20, "ask_size": 20}, "down": {"bid": .37, "ask": .39, "bid_size": 20, "ask_size": 20}},
        },
        "model": {"learning_status": {
            "heldout_report": {"profitable_edge_demonstrated": True, "protocol": HOLD_PROTOCOL, "chronological_out_of_sample": True, "transaction_costs_included": True, "selection_bias_controlled": True},
            "scalp_heldout_report": {"profitable_edge_demonstrated": True, "protocol": SCALP_PROTOCOL, "chronological_out_of_sample": True, "transaction_costs_included": True, "selection_bias_controlled": True},
        }},
        "edge_scanner": {"sides": {
            "up": {"side": "up", "executable": True, "market_purchase_price": .61, "entry_price_with_slippage": .62, "estimated_fee_per_contract": .02, "available_liquidity": 20, "spread": .01, "model_probability": .75, "net_expected_profit_per_contract": .11},
            "down": {"side": "down", "executable": True, "market_purchase_price": .39, "entry_price_with_slippage": .40, "estimated_fee_per_contract": .02, "available_liquidity": 20, "spread": .02, "model_probability": .25, "net_expected_profit_per_contract": -.17},
        }},
        "scalping_monitor": {"sides": {
            "up": {"ask": .61, "ask_size": 20, "spread": .01, "target_bid": .70, "stop_bid": .55, "slippage": .01, "target_probability": .72, "net_expected_profit": .04, "holding_seconds": 60},
            "down": {"ask": .39, "ask_size": 20, "spread": .02, "target_bid": .46, "stop_bid": .34, "slippage": .01, "target_probability": .55, "net_expected_profit": -.01, "holding_seconds": 60},
        }},
        "decision_intelligence": {"components": {"risk_guardian": {"blockers": [], "signal_kill_switch": {"active": False}}}},
    }


def database():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    create_schema(db)
    db.execute("CREATE TABLE forecast_outcomes(ticker TEXT,origin TEXT,outcome INTEGER)")
    return db


def test_strategies_are_independently_gated_and_best_after_cost_ev_wins():
    value = evaluate(state())
    assert value["recommended_strategy"] == "HOLD"
    assert value["action"] == "BUY UP"
    assert value["strategies"]["HOLD"]["hypothetical_exposure"] <= 2
    assert value["strategies"]["SCALP"]["actionable"] is True
    assert value["execution"] == "MANUAL_ONLY"

    unvalidated = state()
    del unvalidated["model"]["learning_status"]["scalp_heldout_report"]
    result = evaluate(unvalidated)
    assert result["strategies"]["SCALP"]["action"] == "WAIT"
    assert result["strategies"]["HOLD"]["action"] == "BUY UP"


@pytest.mark.parametrize("change", ["stale", "spread", "loss_limit", "kill_switch"])
def test_risk_filters_fail_closed(change):
    value = state()
    performance_report = None
    if change == "stale": value["stale"] = True
    if change == "spread": value["edge_scanner"]["sides"]["up"]["spread"] = .20
    if change == "loss_limit": performance_report = {"SCALP": {"today_net_profit": -3}, "HOLD": {"today_net_profit": -2}}
    if change == "kill_switch": value["decision_intelligence"]["components"]["risk_guardian"]["signal_kill_switch"]["active"] = True
    result = evaluate(value, paper_performance=performance_report)
    if change == "spread":
        assert result["strategies"]["HOLD"]["action"] == "WAIT"
    else:
        assert result["action"] == "WAIT"


def test_signal_and_outcome_are_append_only_and_repeated_states_do_not_duplicate():
    db = database()
    value = state()
    # Make SCALP the preferred independently validated strategy.
    value["edge_scanner"]["sides"]["up"]["net_expected_profit_per_contract"] = .01
    dual = evaluate(value)
    value["dual_strategy"] = dual
    record_and_resolve(db, value, NOW)
    record_and_resolve(db, value, NOW + timedelta(seconds=1))
    assert db.execute("SELECT count(*) FROM strategy_paper_signals").fetchone()[0] == 1
    value["now"] = (NOW + timedelta(seconds=5)).isoformat()
    value["market"]["quotes"]["up"]["bid"] = .72
    record_and_resolve(db, value, NOW + timedelta(seconds=5))
    assert db.execute("SELECT count(*) FROM strategy_paper_outcomes").fetchone()[0] == 1
    report = performance(db)["SCALP"]
    assert report["settled_or_observed_outcomes"] == 1
    assert report["net_profit"] is not None
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE strategy_paper_signals SET entry=0")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM strategy_paper_outcomes")


def test_hold_outcome_requires_official_settlement_and_never_invents_fill():
    db = database()
    value = state()
    value["dual_strategy"] = evaluate(value)
    record_and_resolve(db, value, NOW)
    assert db.execute("SELECT count(*) FROM strategy_paper_outcomes").fetchone()[0] == 0
    db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?)", (TICKER, "official", 1))
    record_and_resolve(db, value, NOW + timedelta(minutes=11))
    row = db.execute("SELECT outcome_type,payload FROM strategy_paper_outcomes").fetchone()
    assert row["outcome_type"] == "OFFICIAL_SETTLEMENT"
    assert '"actual_fill":false' in row["payload"]

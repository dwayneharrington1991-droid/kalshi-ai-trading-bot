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
    assert value["action"] == "PAPER BUY UP"
    assert value["strategies"]["HOLD"]["hypothetical_exposure"] <= 1
    assert value["strategies"]["SCALP"]["paper_actionable"] is True
    assert value["strategies"]["SCALP"]["actionable"] is False
    assert value["execution"] == "AUTOMATED_PAPER_ONLY"
    assert value["execution_capability"] is False

    unvalidated = state()
    del unvalidated["model"]["learning_status"]["scalp_heldout_report"]
    result = evaluate(unvalidated)
    assert result["strategies"]["SCALP"]["action"] == "PAPER BUY UP"
    assert result["strategies"]["SCALP"]["validation"]["status"] == "SHADOW_UNVALIDATED"
    assert result["strategies"]["HOLD"]["action"] == "PAPER BUY UP"


@pytest.mark.parametrize("change", ["stale", "spread", "loss_limit", "kill_switch"])
def test_risk_filters_fail_closed(change):
    value = state()
    performance_report = None
    if change == "stale": value["stale"] = True
    if change == "spread": value["edge_scanner"]["sides"]["up"]["spread"] = .20
    if change == "loss_limit": performance_report = {"SCALP": {"today_net_profit": -2}, "HOLD": {"today_net_profit": 0}}
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
    assert report["total_return"] is not None
    assert performance(db)["portfolio"]["total_return"] is not None
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
    assert '"exit_fee":0.0' in row["payload"]
    assert '"actual_fill":false' in row["payload"]


def test_daily_paper_pnl_rolls_over_with_evaluation_date():
    db = database()
    value = state()
    value["edge_scanner"]["sides"]["up"]["net_expected_profit_per_contract"] = .01
    value["dual_strategy"] = evaluate(value)
    record_and_resolve(db, value, NOW)
    value["market"]["quotes"]["up"]["bid"] = .50
    record_and_resolve(db, value, NOW + timedelta(seconds=5))

    same_day = performance(db, asof=NOW + timedelta(hours=1))["SCALP"]
    next_day = performance(db, asof=NOW + timedelta(days=1))["SCALP"]
    assert same_day["today_net_profit"] < 0
    assert next_day["today_net_profit"] == 0


def test_database_boundary_enforces_total_exposure_and_no_averaging_down():
    db = database()
    value = state()
    value["dual_strategy"] = evaluate(value)
    record_and_resolve(db, value, NOW)
    assert db.execute("SELECT COUNT(*) FROM strategy_paper_signals").fetchone()[0] == 1

    # A different strategy cannot add to the same contract side.
    duplicate = state()
    duplicate["edge_scanner"]["sides"]["up"]["net_expected_profit_per_contract"] = 0.01
    duplicate["dual_strategy"] = evaluate(duplicate)
    record_and_resolve(db, duplicate, NOW + timedelta(seconds=1))
    assert db.execute("SELECT COUNT(*) FROM strategy_paper_signals").fetchone()[0] == 1

    # Independently forged oversized paper state is rejected by persistence.
    oversized = state()
    oversized["market"]["ticker"] = TICKER + "-OTHER"
    oversized["dual_strategy"] = evaluate(oversized)
    selected = oversized["dual_strategy"]["strategies"]["HOLD"]
    selected["hypothetical_exposure"] = 5.01
    selected["paper_actionable"] = True
    oversized["dual_strategy"]["recommended_strategy"] = "HOLD"
    record_and_resolve(db, oversized, NOW + timedelta(seconds=2))
    assert db.execute("SELECT COUNT(*) FROM strategy_paper_signals").fetchone()[0] == 1


def test_database_boundary_enforces_five_dollar_portfolio_cap_and_stale_block():
    db = database()
    for index in range(8):
        value = state()
        value["market"]["ticker"] = f"{TICKER}-{index}"
        value["dual_strategy"] = evaluate(value)
        record_and_resolve(db, value, NOW + timedelta(seconds=index))

    rows = db.execute(
        "SELECT COUNT(*),SUM(exposure) FROM strategy_paper_signals"
    ).fetchone()
    assert rows[0] == 7
    assert rows[1] <= 5

    stale = state()
    stale["market"]["ticker"] = TICKER + "-STALE"
    stale["dual_strategy"] = evaluate(stale)
    # Persistence rejects stale input even if an upstream object is forged.
    stale["stale"] = True
    selected = stale["dual_strategy"]["strategies"]["HOLD"]
    selected["paper_actionable"] = True
    stale["dual_strategy"]["recommended_strategy"] = "HOLD"
    record_and_resolve(db, stale, NOW + timedelta(seconds=9))
    assert db.execute("SELECT COUNT(*) FROM strategy_paper_signals").fetchone()[0] == 7


def test_database_boundary_enforces_two_dollar_daily_loss_stop():
    db = database()
    value = state()
    value["market"]["ticker"] = TICKER + "-PRIOR"
    value["dual_strategy"] = evaluate(value)
    record_and_resolve(db, value, NOW)
    key = db.execute("SELECT signal_key FROM strategy_paper_signals").fetchone()[0]
    db.execute(
        "INSERT INTO strategy_paper_outcomes VALUES (?,?,?,?,?,?,?)",
        (key, NOW.isoformat(), "OFFICIAL_SETTLEMENT", 0, -2, 0, "{}"),
    )

    blocked = state()
    blocked["market"]["ticker"] = TICKER + "-AFTER-LOSS"
    blocked["dual_strategy"] = evaluate(blocked)
    record_and_resolve(db, blocked, NOW + timedelta(seconds=1))
    assert db.execute("SELECT COUNT(*) FROM strategy_paper_signals").fetchone()[0] == 1


def test_immutable_candlestick_opening_candidate_drives_paper_hold_only():
    value = state()
    value["prediction_engine"] = {
        "initial": {
            "ticker": TICKER,
            "model": "opening-candidate/abc123",
            "direction": "UP",
            "up": 0.80,
        }
    }
    result = evaluate(value)
    hold = result["strategies"]["HOLD"]
    assert hold["paper_actionable"] is True
    assert hold["action"] == "PAPER BUY UP"
    assert hold["signal_source"] == "immutable_candlestick_opening_candidate"
    assert hold["actionable"] is False


def test_wait_explains_exact_missing_strategy_inputs():
    value = state()
    value["edge_scanner"]["sides"] = {
        "up": {"executable": False},
        "down": {"executable": False},
    }
    value["scalping_monitor"]["sides"] = {
        "up": {"ask": .61},
        "down": {},
    }
    result = evaluate(value)
    assert result["action"] == "WAIT"
    assert result["input_diagnostics"]["HOLD"][
        "missing_executable_quote_or_probability"
    ] == 2
    assert result["input_diagnostics"]["SCALP"][
        "incomplete_target_stop_quote"
    ] == 2
    assert "missing_executable_quote_or_probability=2" in result["strategies"]["HOLD"]["reason"]
    assert "incomplete_target_stop_quote=2" in result["strategies"]["SCALP"]["reason"]

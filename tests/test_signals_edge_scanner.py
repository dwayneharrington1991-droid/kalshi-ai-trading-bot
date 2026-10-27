"""Executable-price edge math uses synthetic data and never calls an API."""
from datetime import timedelta
import sqlite3

import pytest

from src.signals_display.edge_scanner import (
    PROTOCOL,
    calculate,
    create_schema,
    performance,
    quantity_fee,
    record,
)
from tests.test_signals_display import NOW


def state():
    return {
        "now": NOW.isoformat(),
        "mode": "PUBLIC MARKET DATA",
        "stale": False,
        "remaining_seconds": 600,
        "market": {
            "ticker": "KXBTC15M-EDGE",
            "observed_at": NOW.isoformat(),
            "closes_at": (NOW + timedelta(minutes=10)).isoformat(),
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "quotes": {
                "up": {"bid": 0.48, "ask": 0.52, "ask_size": 12},
                "down": {"bid": 0.46, "ask": 0.54, "ask_size": 7},
            },
        },
        "prediction_engine": {
            "ticker": "KXBTC15M-EDGE",
            "status": "EXPERIMENTAL",
            "up": 0.60,
            "down": 0.40,
            "updated_at": NOW.isoformat(),
        },
        "model": {"learning_status": {}, "analysis_24h": {"regime": "normal"}},
    }


def test_edge_uses_executable_ask_costs_depth_and_never_calls_it_verified():
    result = calculate(state(), slippage=0.01, now=NOW)
    up = result["sides"]["up"]
    assert up["market_purchase_price"] == 0.52
    assert up["market_implied_probability"] == 0.52
    assert up["probability_edge"] == pytest.approx(0.08)
    assert up["entry_price_with_slippage"] == 0.53
    assert up["estimated_fee_per_contract"] == quantity_fee(0.53, 1, 1)
    assert up["break_even_probability"] == pytest.approx(0.55)
    assert up["net_expected_profit_per_contract"] == pytest.approx(0.05)
    assert up["maximum_executable_quantity_at_displayed_price"] == 12
    assert up["status"] == "EXPERIMENTAL_POSITIVE_EV"
    assert result["label"] == "NO VERIFIED EDGE — WAIT"
    assert result["actionable"] is False


@pytest.mark.parametrize("change", [
    {"stale": True},
    {"market": {"observed_at": (NOW - timedelta(seconds=6)).isoformat()}},
    {"market": {"fee_type": "unknown"}},
])
def test_stale_or_unsupported_inputs_fail_closed(change):
    value = state()
    if "market" in change:
        value["market"].update(change["market"])
    else:
        value.update(change)
    result = calculate(value, now=NOW)
    assert result["actionable"] is False
    assert all(not side["executable"] for side in result["sides"].values())


def test_validation_gate_requires_explicit_matching_oos_protocol():
    value = state()
    value["model"]["learning_status"]["heldout_report"] = {
        "profitable_edge_demonstrated": True,
        "protocol": PROTOCOL,
        "chronological_out_of_sample": True,
        "transaction_costs_included": True,
        "selection_bias_controlled": True,
    }
    result = calculate(value, now=NOW)
    assert result["validation"]["verified"] is True
    assert result["label"] == "POSITIVE EDGE — UP"


def test_position_comparison_uses_confirmed_account_values_separately():
    account = {
        "live_trade_cards": [{
            "holding_id": "h1", "contract": "KXBTC15M-EDGE", "side": "up",
            "filled_quantity": 2, "total_entry_cost": 1.0,
            "estimated_exit_net_pnl": 0.1, "current_executable_exit_price": 0.57,
            "values_stale": False,
        }],
        "completed_trades": [{"realized_pnl": 0.25}],
    }
    result = calculate(state(), account=account, now=NOW)
    position = result["positions"]["open"][0]
    assert position["estimated_hold_value"] == pytest.approx(1.2)
    assert position["estimated_exit_now_value_after_fees"] == pytest.approx(1.1)
    assert position["guidance"] == "HOLD"
    assert result["positions"]["actual_completed_trade_realized_pnl"] == 0.25


def test_forward_observations_are_timestamped_and_scored_after_official_result():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    create_schema(db)
    db.execute("""CREATE TABLE forecast_outcomes(
      ticker TEXT, origin TEXT, outcome INTEGER, observed_at TEXT)""")
    value = state()
    value["edge_scanner"] = calculate(value, now=NOW)
    record(db, value, NOW)
    assert db.execute("SELECT count(*) FROM edge_observations").fetchone()[0] == 2
    db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", (
        "KXBTC15M-EDGE", "official", 1, (NOW + timedelta(minutes=11)).isoformat()))
    report = performance(db)
    assert report["status"] == "READY"
    assert report["all"]["samples"] == 2
    assert report["profitable_edge_demonstrated"] is False
    assert report["actual_account_results_included"] is False

"""Diagnose existing scalp gates with synthetic state only; no broker access."""
from copy import deepcopy
from datetime import timedelta
import math

import pytest

from src.signals_display.live_model import LiveEstimator
from src.signals_display.model import Display
from src.signals_display.monitor import analyze
from src.signals_display.scalp_monitor import ScalpCriteria, diagnose
from tests.test_signals_display import NOW
from tests.test_signals_live_model import live_frame, estimate
from tests.test_signals_monitor import frame


def ready_state():
    data = frame()
    data["markets"][0].update(mapping_verified=True, averaging_rule_verified=True)
    for quote in data["markets"][0]["quotes"].values():
        quote.update(bid=0.49, ask=0.51, bid_size=20, ask_size=20)
    info = {
        "enabled": True,
        "version": "synthetic-one-contract-diffusion",
        "validated": False,
        "scalp_reason": "MODEL_ESTIMATE: synthetic target/stop/timeout paths; edge unverified",
        "execution_assumptions": {"manual_delay_seconds": 3, "slippage_per_leg": 0.01},
        "scalp_edges": {
            "up": {
                "target_probability_given_entry": 0.7,
                "net_expected_profit_per_opportunity": 0.03,
                "net_ev_sampling_interval": [0.01, 0.05],
                "entered_paths": 1000,
            },
            "down": {
                "target_probability_given_entry": 0.3,
                "net_expected_profit_per_opportunity": -0.02,
                "net_ev_sampling_interval": [-0.04, 0],
                "entered_paths": 1000,
            },
        },
    }
    result = Display(
        "ESTIMATE",
        "Synthetic only",
        settlement_up=0.6,
        settlement_down=0.4,
        scalp_up=0.7,
        scalp_down=0.3,
    )
    state = analyze(data, NOW, 0.05, 0.10, 60, result, info)
    state["model"]["scanner"] = {
        "timeframe_returns": {"1m": 0.001, "3m": 0.002, "5m": 0.003},
        "structural_candidate": {"up": True, "down": False},
        "macro_blackout": False,
        "macro_calendar_status": "Only post-release coverage",
    }
    state["trade_decision"] = {
        "promotion_status": "BLOCKED: no reviewed evidence of profitable execution after costs"
    }
    return state


def codes(result, side=None):
    return {b["code"] for b in result["blockers"] if side is None or b.get("side") == side}


def test_hypothetical_candidate_never_becomes_a_validated_recommendation():
    state = ready_state()
    original = deepcopy(state)
    result = diagnose(state, now=NOW)
    assert result["engine_status"] == "ACTIVE" and result["data_status"] == "FRESH"
    assert result["signal_status"] == "CANDIDATE" and result["candidate_side"] == "up"
    assert result["liquidity_status"] == "SUFFICIENT" and result["spread"]["up"] == pytest.approx(
        0.02
    )
    assert result["recommendation"] == "WAIT" and not result["actionable"]
    assert result["edge_status"] == "EDGE UNVERIFIED" and result["model_status"] == "EXPERIMENTAL"
    assert "REVIEWED_SCALP_PROMOTION" in codes(result)
    assert result["exposure"]["confirmed_account_quantity"] is None
    assert result["sides"]["up"]["entry_fee"] == 0.02
    assert result["sides"]["up"]["entry_cap"] == 0.52
    assert not result["sides"]["up"]["entry_qualified"]
    assert state == original


def test_settlement_validation_flag_cannot_validate_scalp_profitability():
    state = ready_state()
    state["model"].update(
        validated=True,
        learning_status={
            "trained_candidate_loaded": True,
            "heldout_report": {"status": "EXPERIMENTAL_CANDIDATE"},
        },
    )
    state["edges"]["profitable_edge_demonstrated"] = True
    result = diagnose(state, now=NOW)
    assert result["model_status"] == "EXPERIMENTAL" and not result["actionable"]
    assert "settlement" in result["model_validation_reason"]


@pytest.mark.parametrize(
    "change,code",
    [
        (
            lambda s: s["model"].update(enabled=False, scalp_reason="MODEL_DISABLED"),
            "ENGINE_ENABLED",
        ),
        (
            lambda s: s["market"].update(observed_at=(NOW - timedelta(seconds=6)).isoformat()),
            "QUOTE_FRESHNESS",
        ),
        (
            lambda s: s["reference"].update(observed_at=(NOW + timedelta(seconds=1)).isoformat()),
            "REFERENCE_FRESHNESS",
        ),
        (lambda s: s["market"].update(closes_at=NOW.isoformat()), "CONTRACT_LIFETIME"),
        (lambda s: s["market"].update(fee_type="unknown"), "SUPPORTED_FEE_SCHEDULE"),
        (lambda s: s["market"]["quotes"]["up"].update(ask_size=None), "CURRENT_LIQUIDITY"),
        (lambda s: s["market"]["quotes"]["up"].update(ask=0.55), "SPREAD_LIMIT"),
        (
            lambda s: s["market"].update(quote_event_time=(NOW + timedelta(seconds=1)).isoformat()),
            "QUOTE_EVENT_FRESHNESS",
        ),
        (lambda s: s["model"]["scanner"].update(macro_blackout=True), "MACRO_BLACKOUT"),
    ],
)
def test_diagnostics_name_concrete_blocker_without_bypassing_it(change, code):
    state = ready_state()
    change(state)
    result = diagnose(state, now=NOW)
    assert code in codes(result) and result["signal_status"] == "BLOCKED"
    assert result["exact_reason"] and result["recommendation"] == "WAIT"


def test_stale_reference_clears_current_probabilities_and_edge_despite_fresh_book():
    state = ready_state()
    state["reference"]["observed_at"] = (NOW - timedelta(seconds=6)).isoformat()
    result = diagnose(state, now=NOW)
    assert result["data_status"] == "STALE"
    assert result["estimated_edge"] == {"up": None, "down": None}
    assert result["sides"]["up"]["target_probability"] is None
    assert result["data_ages"]["quote_seconds"] == 0


def test_source_skew_can_be_stricter_without_changing_model_or_risk_limits():
    state = ready_state()
    state["reference"]["observed_at"] = (NOW - timedelta(seconds=2)).isoformat()
    limits = {"max_account_quantity": 100}
    account = {"status": "READ_ONLY_CONNECTED", "limits": limits, "holdings": []}
    result = diagnose(state, {"maximum_source_skew_seconds": 1}, account, NOW)
    assert result["data_status"] == "STALE" and "SOURCE_SYNCHRONIZATION" in codes(result)
    assert account["limits"] == limits


def test_warmup_reports_actual_estimator_history_and_installed_thresholds():
    payload = live_frame(0)
    model = LiveEstimator()
    result, info = estimate(model, payload)
    state = analyze(payload, NOW, model_result=result, model_info=info)
    diagnostic = diagnose(state, now=NOW)
    assert diagnostic["engine_status"] == "ACTIVE"
    assert diagnostic["signal_status"] == "WAIT"
    assert diagnostic["quote_history"]["samples"] == 1
    assert "1/61 samples" in diagnostic["exact_reason"]
    assert "SCALP_ESTIMATOR_READY" in codes(diagnostic)
    assert diagnostic["criteria"]["minimum_entered_paths"] == 200


def test_high_success_probability_with_negative_cost_adjusted_ev_stays_wait():
    state = ready_state()
    state["model"]["scalp_edges"]["up"].update(
        target_probability_given_entry=0.99,
        net_expected_profit_per_opportunity=-0.001,
        net_ev_sampling_interval=[-0.002, 0],
    )
    result = diagnose(state, now=NOW)
    assert result["signal_status"] == "WAIT" and not result["hypothetical_candidates"]
    assert "NET_EXPECTED_VALUE" in codes(result, "up")
    assert "fees" in result["exact_reason"]


def test_quantity_incompatible_with_one_contract_mc_is_not_scaled_into_an_edge():
    state = ready_state()
    result = diagnose(state, {"quantity": 2}, now=NOW)
    assert result["signal_status"] == "BLOCKED"
    assert "MODELED_QUANTITY" in codes(result, "up")
    assert result["estimated_edge"]["up"] is None
    assert result["sides"]["up"]["target_probability"] is None
    assert result["sides"]["up"]["entry_fee"] == 0.04


def test_confirmed_holdings_reservations_and_liquidation_values_remain_distinct():
    state = ready_state()
    account = {
        "status": "READ_ONLY_CONNECTED",
        "observed_at": NOW.isoformat(),
        "available_balance_dollars": 100,
        "holdings": [
            {"ticker": state["market"]["ticker"], "side": "up", "status": "OPEN", "quantity": 2},
            {"ticker": "KXBTC15M-OTHER", "side": "down", "status": "CLOSED", "quantity": 8},
        ],
        "pending_orders": [{"ticker": state["market"]["ticker"], "remaining_quantity": "3"}],
        "limits": {
            "max_add_quantity": 1,
            "max_account_quantity": 5,
            "max_per_contract_quantity": 5,
            "max_add_cost_dollars": 10,
            "minimum_cash_reserve": 1,
        },
        "live_trade_cards": [
            {
                "holding_id": 1,
                "contract": state["market"]["ticker"],
                "side": "up",
                "filled_quantity": 2,
                "current_executable_exit_price": 0.49,
                "total_entry_cost": 1.1,
                "estimated_exit_net_pnl": -0.18,
                "remaining_cost_at_risk": 1.1,
                "guidance": "CONSIDER EXIT",
                "reason": "Recorded stop reached",
            }
        ],
    }
    result = diagnose(state, account=account, now=NOW)
    exposure = result["exposure"]
    assert (
        exposure["confirmed_account_quantity"] == 2 and exposure["unfilled_reserved_quantity"] == 3
    )
    assert exposure["positions"][0]["estimated_liquidation_value_after_costs"] == pytest.approx(
        0.92
    )
    assert not result["sides"]["up"][
        "risk_qualified"
    ]  # Pending quantity already consumes the limit.
    assert not result["actionable"]
    account["status"] = "ACCOUNT_STALE"
    stale = diagnose(state, account=account, now=NOW)
    assert stale["exposure"]["confirmed_account_quantity"] is None
    assert stale["exposure"]["positions"][0]["estimated_liquidation_value_after_costs"] is None
    assert stale["exposure"]["positions"][0]["guidance"] == "WAIT"


@pytest.mark.parametrize(
    "mapping",
    [
        {"minimum_success_probability": 0.5},
        {"maximum_spread": 0.04},
        {"minimum_entered_paths": 199},
        {"maximum_data_age_seconds": 6},
        {"minimum_net_ev": -0.01},
        {"quantity": True},
        {"quantity": math.nan},
        {"minimum_entered_paths": 200.0},
        {"maximum_holding_seconds": 0},
        {"unknown": 1},
    ],
)
def test_criteria_reject_quality_relaxation_and_invalid_input(mapping):
    with pytest.raises(ValueError):
        ScalpCriteria.from_mapping(mapping)


def test_stricter_criteria_are_explicit_and_do_not_change_economic_inputs():
    state = ready_state()
    criteria = ScalpCriteria.from_mapping(
        {"minimum_success_probability": 0.8, "maximum_spread": 0.02}
    )
    result = diagnose(state, criteria, now=NOW)
    assert result["signal_status"] == "WAIT"
    assert result["criteria"]["minimum_success_probability"] == 0.8
    assert result["sides"]["up"]["entry_cap"] == 0.52

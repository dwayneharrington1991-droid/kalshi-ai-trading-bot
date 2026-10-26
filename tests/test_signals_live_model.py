"""Deterministic synthetic observations, no live API or credentials."""
from copy import deepcopy
from datetime import timedelta
import math

import pytest

from src.signals_display.live_model import LiveEstimator, volatility
from src.signals_display.model import timestamp
from src.signals_display.monitor import Journal, PublicFeed, active_market, analyze
from src.signals_display.server import Monitor
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def live_frame(i):
    payload = frame(2 * i)
    payload["mode"] = "PUBLIC MARKET DATA"
    market = payload["markets"][0]
    market.update(mapping_verified=True, averaging_rule_verified=True)
    mid = 0.5 + 0.04 * math.sin(i * 0.8)
    market["quotes"]["up"].update(bid=mid - 0.01, ask=mid + 0.01)
    market["quotes"]["down"].update(bid=1 - mid - 0.01, ask=1 - mid + 0.01)
    payload["reference"].update(
        price=100000 + 5 * math.sin(i * 0.4), official=False, source="Coinbase synthetic proxy"
    )
    return payload


def trained():
    model = LiveEstimator()
    for i in range(61):
        payload = live_frame(i)
        model.observe(payload, timestamp(payload["now"]))
    payload["reference"]["price"] = 100000
    return model, payload


def estimate(model, payload, **kwargs):
    now = timestamp(payload["now"])
    return model.estimate(
        payload,
        active_market(payload, now),
        now,
        kwargs.get("target", 0.05),
        kwargs.get("stop", 0.10),
        kwargs.get("horizon", 10),
    )


def test_new_public_model_reports_exact_warmup_in_both_panels():
    model = LiveEstimator()
    payload = live_frame(0)
    result, info = estimate(model, payload)
    assert result.settlement_up is result.scalp_up is result.scalp_down is None
    assert "reference 1/61 samples, 0/120s span" in info["overall_reason"]
    assert "contract quotes 1/61 samples, 0/120s span" in info["scalp_reason"]


def test_settlement_at_strike_is_half_and_does_not_require_scalp_fees():
    model, payload = trained()
    payload["markets"][0]["fee_type"] = "unknown"
    result, info = estimate(model, payload)
    assert result.settlement_up == result.settlement_down == 0.5
    assert result.scalp_up is None and "UNKNOWN_FEES" in info["scalp_reason"]
    assert "zero Coinbase/CF Benchmarks basis" in info["settlement_assumptions"]


def test_settlement_is_monotonic_in_strike_and_normal_formula_matches():
    model, payload = trained()
    result, info = estimate(model, payload)
    inputs = info["settlement_inputs"]
    sigma = info["reference_history"]["volatility_per_sqrt_second"]
    remaining = inputs["remaining_seconds"]
    assert inputs["standard_deviation_average"] == pytest.approx(sigma * math.sqrt(remaining - 40))
    payload["markets"][0]["strike"] = 100010
    lower, _ = estimate(model, payload)
    assert 0 < lower.settlement_up < 0.5
    payload["markets"][0]["strike"] = 99990
    higher, _ = estimate(model, payload)
    assert 0.5 < higher.settlement_up < 1


def test_separate_scalp_estimates_are_reproducible_and_include_mc_uncertainty():
    model, payload = trained()
    result, info = estimate(model, payload)
    again, _ = estimate(model, payload)
    assert result == again
    assert 0 <= result.scalp_up <= 1 and 0 <= result.scalp_down <= 1
    assert info["monte_carlo"]["paths"] == 2000
    assert (
        info["monte_carlo"]["up_sampling_interval"][0]
        <= result.scalp_up
        <= info["monte_carlo"]["up_sampling_interval"][1]
    )
    assert info["validated"] is False and "UNVALIDATED" in result.label


def test_spread_and_fees_can_stop_both_at_entry_without_forcing_a_side():
    model, payload = trained()
    result, info = estimate(model, payload, stop=0.01)
    assert result.scalp_up == result.scalp_down == 0
    state = analyze(payload, timestamp(payload["now"]), 0.05, 0.01, 10, result, info)
    assert state["scalp"] == "WAIT" and "no unique side" in state["scalp_explanation"]


def test_unreachable_target_produces_zero_wins_not_discarded_timeouts():
    model, payload = trained()
    result, _ = estimate(model, payload, target=1)
    assert result.scalp_up == result.scalp_down == 0


def test_missing_reference_only_blocks_overall_not_quote_model():
    model, payload = trained()
    payload["reference"] = {}
    result, info = estimate(model, payload)
    assert result.settlement_up is None and "MISSING_REFERENCE" in info["overall_reason"]
    assert result.scalp_up is not None and result.scalp_down is not None


def test_unknown_averaging_rule_does_not_guess_settlement():
    model, payload = trained()
    payload["markets"][0]["averaging_rule_verified"] = False
    result, info = estimate(model, payload)
    assert result.settlement_up is None and "UNSUPPORTED_RULES" in info["overall_reason"]
    assert result.scalp_up is not None


def test_zero_volatility_and_duplicate_timestamps_are_not_history():
    model = LiveEstimator()
    payload = live_frame(0)
    for _ in range(100):
        model.observe(payload, NOW)
    result, info = estimate(model, payload)
    assert info["quote_history"]["samples"] == 1
    constant = [(NOW + timedelta(seconds=2 * i), 100) for i in range(61)]
    sigma, reason, _ = volatility(constant, constant[-1][0], "reference")
    assert sigma is None and "INSUFFICIENT_VARIATION" in reason


def test_gap_resets_history_instead_of_hiding_missing_observations():
    model, _ = trained()
    payload = live_frame(65)
    result, info = estimate(model, payload)
    assert result.settlement_up is result.scalp_up is None
    assert info["reference_history"]["samples"] == info["quote_history"]["samples"] == 1


def test_rollover_preserves_proxy_history_but_requires_new_quote_history():
    model, payload = trained()
    payload["markets"][0]["ticker"] = "KXBTC15M-NEXT"
    result, info = estimate(model, payload)
    assert result.settlement_up is not None and result.scalp_up is None
    assert info["quote_history"]["samples"] == 1


def test_stale_reference_and_stale_quotes_have_independent_reasons():
    model, payload = trained()
    payload["reference"]["observed_at"] = (
        timestamp(payload["now"]) - timedelta(seconds=6)
    ).isoformat()
    result, info = estimate(model, payload)
    assert result.settlement_up is None and "STALE_DATA" in info["overall_reason"]
    assert result.scalp_up is not None
    payload["markets"][0]["observed_at"] = payload["reference"]["observed_at"]
    result, info = estimate(model, payload)
    assert result.scalp_up is None and "STALE_DATA" in info["scalp_reason"]


def test_final_averaging_window_uses_observed_past_not_just_current_price():
    model, payload = trained()
    now = timestamp(payload["now"])
    payload["markets"][0]["closes_at"] = (now + timedelta(seconds=10)).isoformat()
    result, info = estimate(model, payload, horizon=5)
    assert result.settlement_up is not None
    assert (
        info["settlement_inputs"]["standard_deviation_average"]
        < info["reference_history"]["volatility_per_sqrt_second"]
    )
    assert info["settlement_inputs"]["mean_average"] != payload["reference"]["price"]


def test_restore_recovers_observations_but_never_fabricates_forecasts(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    for i in range(61):
        payload = live_frame(i)
        journal.step(analyze(payload, timestamp(payload["now"])))
    model = LiveEstimator()
    model.restore(journal.recent_states())
    payload = live_frame(60)
    result, info = estimate(model, payload)
    assert info["reference_history"]["samples"] == 61
    assert result.settlement_up is not None


def test_explicitly_disabled_model_diagnoses_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("src.signals_display.server.utcnow", lambda: NOW)

    class MockPublicFeed(PublicFeed):
        def poll(self):
            return live_frame(0)

    monitor = Monitor(MockPublicFeed(), Journal(tmp_path / "journal.db"), models_enabled=False)
    monitor.tick()
    state = monitor.snapshot()
    assert "MODEL_DISABLED" in state["explanation"]
    assert "MODEL_DISABLED" in state["scalp_explanation"]
    assert state["estimate"]["settlement_up"] is None


def test_monitor_wires_live_model_and_journals_both_probabilities(tmp_path, monkeypatch):
    payloads = iter(live_frame(i) for i in range(61))

    class MockPublicFeed(PublicFeed):
        def poll(self):
            return next(payloads)

    monitor = Monitor(MockPublicFeed(), Journal(tmp_path / "journal.db"), horizon=10)
    for _ in range(61):
        monitor.tick()
    state = monitor.state
    assert state["estimate"]["settlement_up"] is not None
    assert state["estimate"]["scalp_up"] is not None and state["estimate"]["scalp_down"] is not None
    assert "MODEL_ESTIMATE" in state["explanation"]
    assert state["model"]["enabled"] and not state["model"]["validated"]
    saved = monitor.journal.recent_states()[-1]
    assert saved["model"]["version"] == state["model"]["version"]
    # Slow network/model work cannot republish an old observation as fresh.
    monkeypatch.setattr(
        "src.signals_display.server.utcnow", lambda: timestamp(state["now"]) + timedelta(seconds=6)
    )
    assert monitor.snapshot()["stale"] and monitor.snapshot()["estimate"]["settlement_up"] is None


def test_unknown_depth_is_reported_as_missing_data_not_zero_market_liquidity():
    model, payload = trained()
    payload["markets"][0][
        "book_diagnostic"
    ] = "MISSING_DEPTH: public orderbook unavailable (HTTPStatusError HTTP 429)"
    result, info = estimate(model, payload)
    assert result.settlement_up is not None and result.scalp_up is None
    assert "MISSING_DEPTH" in info["scalp_reason"] and "429" in info["scalp_reason"]

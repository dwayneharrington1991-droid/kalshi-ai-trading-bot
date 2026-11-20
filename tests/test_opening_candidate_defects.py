"""Regression tests for the opening candidate-to-fallback defects.

Each test reproduces one confirmed failure mode of opening_forecast with the existing fixtures.
"""
from datetime import timedelta

from src.signals_display import prediction_engine as engine
from tests.test_signals_opening_models import NOW, eligible_candidate, history, market, opening, points


def test_stale_reference_withholds_the_probability_and_never_uses_the_candidate():
    """A missing fresh BTC reference leaves no probability. Nothing may then be shown as a candidate call."""
    candidate = eligible_candidate()
    result = engine.opening_forecast(
        market(),
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        [],
        NOW + timedelta(seconds=2),
        candidate_artifact=candidate,
    )
    assert result.get("up") is None
    assert "No fresh BTC reference" in result["reason"]
    assert not str(result.get("model", "")).startswith("opening-candidate/")
    assert result.get("direction") not in ("UP", "DOWN")


def test_candidate_with_a_failing_artifact_is_rejected_on_the_record_and_never_partially_applied():
    """Guard: an artifact that fails validation (missing report) must leave the fallback record intact and
    name the rejection. The fallback record carries no decision policy, so the comparison is against it."""
    fallback = opening()
    candidate = eligible_candidate()
    del candidate["report"]
    result = opening(candidate_artifact=candidate)
    assert result["model"] == fallback["model"]
    assert result["direction"] == fallback["direction"]
    assert result.get("up") == fallback.get("up")
    assert result.get("decision_policy") == fallback.get("decision_policy")
    rejection = result["model_selection"]["candidate_rejection"]
    assert rejection.startswith("OPENING_ARTIFACT_INELIGIBLE"), rejection

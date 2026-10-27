"""Offline calibration mathematics and chronology; no account/network access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

import pytest

from src.signals_display.calibration import (
    apply_calibration,
    calibration_report,
    chronological_calibration,
    drift_report,
    fit_isotonic,
    fit_platt,
)


def rows(n=180):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    result = []
    for i in range(n):
        now = start + timedelta(minutes=15 * i)
        result.append(
            {
                "ticker": f"KXBTC15M-{i}",
                "timestamp": now.isoformat(),
                "closes_at": (now + timedelta(minutes=15)).isoformat(),
                "label_available_at": (now + timedelta(minutes=15, seconds=1)).isoformat(),
                "data_cutoff": now.isoformat(),
                "probability": 0.9 if i % 3 else 0.1,
                "outcome": 1 if i % 2 else 0,
                "regime": "high" if i % 2 else "low",
            }
        )
    return result


def test_pava_known_violating_sequence_and_endpoint_clipping():
    artifact = fit_isotonic([0.1, 0.2, 0.3, 0.4], [0, 1, 0, 1])
    assert artifact["y"] == [0, 0.5, 0.5, 1]
    assert artifact["blocks"] == 3
    assert apply_calibration(artifact, 0) == 0
    assert apply_calibration(artifact, 1) == 1
    assert apply_calibration(artifact, 0.25) == pytest.approx(0.5)
    assert apply_calibration(artifact, 0.35) == pytest.approx(0.75)


def test_pava_tied_probabilities_are_weighted_and_constant_inputs_safe():
    artifact = fit_isotonic([0.5, 0.5, 0.5, 0.5], [0, 0, 0, 1])
    assert artifact["x"] == [0.5]
    assert apply_calibration(artifact, 0.9) == 0.25
    assert artifact["samples"] == 4


def test_pava_is_monotone_and_outputs_complementary_probabilities():
    artifact = fit_isotonic([i / 99 for i in range(100)], [i % 2 for i in range(100)])
    ps = [apply_calibration(artifact, i / 100) for i in range(101)]
    assert ps == sorted(ps)
    assert all(0 <= p <= 1 and p + (1 - p) == 1 for p in ps)


def test_platt_softens_overconfidence_without_forcing_direction():
    artifact = fit_platt([0.01, 0.01, 0.99, 0.99] * 30, [0, 1, 0, 1] * 30)
    assert 0 <= artifact["slope"] < 1
    assert 0.3 < apply_calibration(artifact, 0.01) < 0.5
    assert 0.5 < apply_calibration(artifact, 0.99) < 0.7
    assert apply_calibration(artifact, 0.5) == pytest.approx(0.5)


def test_calibrators_reject_invalid_and_malformed_artifacts():
    for ps, ys in [([math.nan], [0]), ([1.2], [1]), ([0.2], [2]), ([True], [0]), ([], [1])]:
        with pytest.raises(ValueError):
            fit_isotonic(ps, ys)
    with pytest.raises(ValueError):
        fit_platt([], [])
    for artifact in [
        {"method": "executable-code"},
        {"method": "isotonic_pava_v1", "x": [0.8, 0.2], "y": [0.1, 0.9]},
        {"method": "isotonic_pava_v1", "x": [0.2, 0.8], "y": [0.9, 0.1]},
        {"method": "platt_monotone_v1", "slope": -1, "intercept": 0},
    ]:
        with pytest.raises(ValueError):
            apply_calibration(artifact, 0.5)


def test_calibration_report_reliability_bins_and_regime_uncertainty():
    report = calibration_report([0.2, 0.2, 0.8, 0.8], [0, 1, 1, 1], ["low", "low", "high", "high"])
    assert report["brier"] == pytest.approx((0.04 + 0.64 + 0.04 + 0.04) / 4)
    assert report["by_volatility_regime"]["high"]["samples"] == 2
    assert report["reliability_diagram"] == report["calibration"]
    assert report["reliability_diagram"][2]["outcome_rate_interval"] is not None
    assert report["edge_status"] == "edge unverified"
    assert "Serial dependence" in report["uncertainty_note"]
    assert calibration_report([], [])["brier"] is None


def test_chronological_calibration_purges_late_labels_and_never_promotes():
    report = chronological_calibration(rows())
    assert report["status"] == "SHADOW_CALIBRATION_EVALUATED"
    assert report["split_samples"] == {"fit": 107, "selection": 35, "test": 36}
    assert report["fit_labels_through"] < rows()[108]["timestamp"]
    assert report["selection_labels_through"] < report["first_test_prediction"]
    assert report["promotion_allowed"] is False
    assert report["active_model_changed"] is False
    assert set(report["test"]) == {"identity_v1", "platt_monotone_v1", "isotonic_pava_v1"}


def test_test_labels_cannot_change_calibrator_selection_or_fitted_artifact():
    original = rows()
    changed = deepcopy(original)
    for row in changed[144:]:
        row["outcome"] = 1 - row["outcome"]
    a, b = chronological_calibration(original), chronological_calibration(changed)
    assert a["selected_before_test"] == b["selected_before_test"]
    assert a["calibration_artifact"] == b["calibration_artifact"]
    assert a["validation"] == b["validation"]


def test_calibration_rejects_future_inputs_labels_and_duplicate_contracts():
    base = rows()
    for field in ("data_cutoff", "model_trained_through"):
        invalid = deepcopy(base)
        invalid[0][field] = invalid[0]["closes_at"]
        with pytest.raises(ValueError, match="Future information"):
            chronological_calibration(invalid)
    with pytest.raises(ValueError, match="future"):
        chronological_calibration(base, asof=base[50]["timestamp"])
    invalid = deepcopy(base)
    invalid[1]["ticker"] = invalid[0]["ticker"]
    with pytest.raises(ValueError, match="per contract"):
        chronological_calibration(invalid)
    with pytest.raises(ValueError, match="chronological"):
        chronological_calibration(base[::-1])


def test_small_and_one_class_calibration_remain_unavailable():
    assert chronological_calibration(rows(50))["status"] == "INSUFFICIENT_CHRONOLOGICAL_LABELS"
    one_class = rows()
    for row in one_class[:108]:
        row["outcome"] = 1
    assert chronological_calibration(one_class)["status"] == "CALIBRATION_FIT_HAS_ONE_CLASS"


def test_drift_equal_distributions_zero_psi_and_reference_only_bins():
    data = [{"features": {"return": i / 100}, "probability": i / 100} for i in range(100)]
    result = drift_report(data, data)
    assert result["status"] == "NO_LARGE_DISTRIBUTION_CHANGE"
    assert result["features"]["return"]["psi"] == 0
    assert result["features"]["probability"]["psi"] == 0
    assert result["automatic_model_action"] is False
    shifted = [{"features": {"return": 10 + i / 100}} for i in range(100)]
    changed = drift_report(data, shifted)
    assert changed["status"] == "DRIFT_WARNING"
    assert changed["features"]["return"]["bin_edges"] == result["features"]["return"]["bin_edges"]
    assert changed["features"]["return"]["current_in_reference_range_fraction"] == 0


def test_drift_constant_feature_detects_both_shift_directions_and_missingness():
    data = [{"features": {"constant": 5}} for _ in range(100)]
    for value in (4, 6):
        current = [{"features": {"constant": value}} for _ in range(100)]
        assert drift_report(data, current)["features"]["constant"]["psi"] > 0.25
    missing = [{"features": {}} for _ in range(100)]
    result = drift_report(data, missing)
    assert result["status"] == "INSUFFICIENT_FEATURE_COVERAGE"
    assert result["features"]["constant"]["current_missing_fraction"] == 1
    assert result["features"]["constant"]["psi"] is None


def test_drift_invalid_numbers_not_treated_as_zero():
    data = [{"features": {"x": i}} for i in range(100)]
    invalid = [{"features": {"x": math.nan}} for _ in range(100)]
    result = drift_report(data, invalid)
    assert result["features"]["x"]["current_available"] == 0
    assert drift_report(data[:5], data)["status"] == "INSUFFICIENT_DRIFT_SAMPLES"
    with pytest.raises(ValueError):
        drift_report(data, data, bins=1)

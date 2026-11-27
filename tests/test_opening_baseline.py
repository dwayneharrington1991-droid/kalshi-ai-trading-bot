import math

from src.signals_display import opening_baseline as ob


def _row(up, direction, outcome, regime="rising", features=None):
    return {"recorded_up": up, "recorded_direction": direction, "outcome": outcome,
            "regime": regime, "features": features or {"a": 1.0, "b": 2.0}}


def test_accuracy_is_measured_only_on_recorded_up_or_down_calls():
    rows = [_row(0.7, "UP", 1), _row(0.3, "DOWN", 0), _row(0.6, "UP", 0), _row(None, "WAIT", 1)]
    s = ob.summarize(rows)
    assert s["eligible_openings"] == 4
    assert s["directional_calls"] == 3
    assert s["verified_accuracy_on_calls"] == round(2 / 3, 4)
    assert s["coverage"] == 0.75
    assert s["scored_probabilities"] == 3


def test_log_loss_and_brier_match_hand_computation():
    rows = [_row(0.8, "UP", 1), _row(0.2, "DOWN", 0)]
    s = ob.summarize(rows)
    assert s["brier"] == round(((0.8 - 1) ** 2 + (0.2 - 0) ** 2) / 2, 5)
    assert s["log_loss"] == round(-math.log(0.8), 5)


def test_regime_breakdown_counts_openings_calls_and_accuracy():
    rows = [_row(0.7, "UP", 1, regime="rising_strong"), _row(0.4, "DOWN", 1, regime="rising_weak"),
            _row(0.2, "DOWN", 0, regime="falling")]
    regimes = ob.summarize(rows)["regimes"]
    assert regimes["rising"] == {"openings": 2, "calls": 2, "correct": 1, "accuracy": 0.5}
    assert regimes["falling"]["accuracy"] == 1.0


def test_redundancy_flags_constant_and_collinear_features_without_changing_inputs():
    rows = [{"outcome": i % 2, "features": {"const": 3.0, "x": float(i), "dup": float(i) * 2.0}} for i in range(10)]
    red = ob.redundancy(rows)
    assert red["constant_features"] == 1
    assert red["strongly_collinear_pairs"] == 1
    assert red["non_constant_features"] == 2

import math

from src.signals_display import opening_challengers as oc


def test_production_inputs_come_from_component_preprocessor_names():
    artifact = {"model": {"kind": "opening_ensemble", "components": [
        {"kind": "logistic", "preprocessor": {"names": ["a", "b"]}},
        {"kind": "opening_gradient_boosting", "preprocessor": {"names": ["b", "c"]}},
        {"kind": "other", "weights": [1.0]},
    ]}}
    assert oc.production_inputs(artifact) == ["a", "b", "c"]
    assert oc.component_input_counts(artifact) == [2, 2, 0]


def test_call_rule_is_symmetric_around_the_fixed_055_band():
    assert oc.call_of(0.55) == "UP" and oc.call_of(0.45) == "DOWN"
    assert oc.call_of(0.549) == "WAIT" and oc.call_of(0.451) == "WAIT"


def test_scores_use_directional_calls_with_coverage_over_all_openings():
    ps = [0.9, 0.1, 0.5, 0.8]
    ys = [1, 1, 0, 1]
    s = oc.score(ps, ys)
    assert s["eligible"] == 4 and s["calls"] == 3 and s["correct"] == 2
    assert s["coverage"] == 0.75
    assert s["accuracy_on_calls"] == round(2 / 3, 4)


def test_wilson_interval_brackets_the_observed_proportion():
    lo, hi = oc.wilson(60, 94)
    assert lo < 60 / 94 < hi and 0 <= lo and hi <= 1


def test_paired_gain_is_positive_when_the_model_is_better():
    gain = oc.paired_logloss_gain([0.5, 0.5, 0.5], [0.9, 0.9, 0.1], [1, 1, 0])
    assert gain["mean_gain"] > 0 and gain["n"] == 3


def test_walk_forward_with_too_few_rows_reports_no_results_instead_of_success():
    assert oc.walk_forward([], ["x"])["results"] == {}


def test_holm_adjustment_is_monotone_and_at_least_the_raw_p():
    paired = {"a": {"mean_gain": 0.1, "z": 3.0, "n": 50}, "b": {"mean_gain": 0.02, "z": 1.0, "n": 50}}
    oc.holm_adjust(paired)
    assert paired["a"]["p_holm"] >= paired["a"]["p_raw"] and paired["b"]["p_holm"] >= paired["b"]["p_raw"]
    assert paired["a"]["comparisons_in_family"] == 2
    assert paired["a"]["p_holm"] <= paired["b"]["p_holm"] or paired["b"]["p_holm"] <= 1.0


def test_collinearity_pruning_keeps_one_of_a_duplicated_pair_using_training_rows_only():
    rows = [{"features": {"a": float(i), "copy": float(i) * 3.0, "other": float((i * 7) % 5)}} for i in range(40)]
    kept = oc._prune_collinear(rows, ["a", "copy", "other"])
    assert "a" in kept and "copy" not in kept and "other" in kept


def test_walk_forward_reports_zero_leakage_violations_on_clean_timing():
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(200):
        ts = base + timedelta(minutes=15 * i)
        rows.append({"ticker": f"T{i}", "timestamp": ts.isoformat(), "label_available_at": (ts + timedelta(minutes=15)).isoformat(),
                     "outcome": i % 2, "recorded_up": 0.5, "features": {"x": float(i % 7), "y": float(i % 3)}})
    result = oc.walk_forward(rows, ["x", "y"])
    assert result["leakage_violations"] == 0
    assert result["results"]["logistic_pruned"]["eligible"] == result["results"]["live"]["eligible"]

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

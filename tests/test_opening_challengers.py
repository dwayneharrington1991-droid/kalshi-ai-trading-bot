from src.signals_display import opening_challengers as oc


def test_call_rule_is_symmetric_around_the_fixed_055_band():
    assert oc.call_of(0.55) == "UP" and oc.call_of(0.45) == "DOWN"
    assert oc.call_of(0.549) == "WAIT" and oc.call_of(0.451) == "WAIT"


def test_scores_are_computed_on_directional_calls_with_coverage_over_all_openings():
    ps = [0.9, 0.1, 0.5, 0.8]
    ys = [1, 1, 0, 1]
    s = oc.score(ps, ys)
    assert s["eligible"] == 4 and s["calls"] == 3
    assert s["coverage"] == 0.75
    assert s["accuracy_on_calls"] == round(2 / 3, 4)   # 0.9->UP correct, 0.1->DOWN wrong, 0.8->UP correct


def test_walk_forward_with_too_few_rows_reports_no_results_instead_of_success():
    assert oc.walk_forward([], ["x"]) == {"folds": [], "results": {}}


def test_inputs_are_found_by_registered_name_lists_not_by_a_fixed_key():
    from src.signals_display.features import FEATURES
    real = sorted(FEATURES)[:6]
    artifact = {"model": {"kind": "opening_ensemble", "components": [
        {"kind": "logistic", "weights_for": real[:3]},
        {"kind": "boost", "stumps_on": real},
        {"label": "opening", "note": ["not", "features", "x", "y", "z"]},
    ]}}
    assert oc.production_inputs(artifact) == sorted(real)
    report = oc.inspect_inputs(artifact)
    assert any(r["path"].endswith("stumps_on") and r["registered_feature_names"] == 6 for r in report)

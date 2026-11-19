from src.signals_display import opening_challengers as oc


def test_production_inputs_are_the_union_of_component_prep_names():
    artifact = {"model": {"kind": "opening_ensemble", "components": [
        {"kind": "logistic", "prep": {"names": ["a", "b"]}},
        {"kind": "opening_gradient_boosting", "prep": {"names": ["b", "c"]}},
    ]}}
    assert oc.production_inputs(artifact) == ["a", "b", "c"]


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

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import research_order_flow_value as research


START = datetime(2026, 11, 19, tzinfo=timezone.utc)


def test_matched_forward_comparison_purges_labels_not_yet_received():
    rows = [{"sample_id": str(i), "decision_at": START + timedelta(minutes=i),
             "label_at": START + timedelta(minutes=i + 2), "y": i % 2, "baseline": .5,
             "features": {"price.a": i % 3, "flow.b": i % 2}} for i in range(25)]
    rows[-1]["features"].pop("flow.b")
    predictions, counts = research.walk_forward(rows, {
        "eval_start": rows[22]["decision_at"], "eval_end": START + timedelta(hours=1),
        "min_train": 20, "l2": 10, "candidates": {"price_only": ["price.a"], "flow": ["price.a", "flow.b"]},
    })
    assert counts["evaluation_records"] == 3 and counts["matched_feature_records"] == 2
    assert [row["train_n"] for row in predictions] == [20, 21]
    assert all(set(row["p"]) == {"price_only", "flow"} for row in predictions)


def test_loader_rejects_protected_decisions_and_late_features(tmp_path):
    path = tmp_path / "research.jsonl"
    row = {"sample_id": "x", "target": "opening_final", "decision_at": START.isoformat(),
           "label_observed_at": (START + timedelta(minutes=15)).isoformat(),
           "baseline_generated_at": START.isoformat(), "baseline_probability": .5,
           "label": "UP", "families": {"flow": {
               "received_at": (START + timedelta(seconds=1)).isoformat(), "values": {"cvd": 0.1}}}}
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="received after"):
        research.load_records(path, {"target": "opening_final"})
    row["decision_at"] = research.DEVELOPMENT_END.isoformat()
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="documented development period"):
        research.load_records(path, {"target": "opening_final"})


@pytest.mark.parametrize("target", list(research.TARGETS))
def test_competition_scores_recorded_calls_and_separates_each_target(tmp_path, target):
    first, last, label_delay, source = research.TARGETS[target]
    export, spec = tmp_path / "development.jsonl", tmp_path / "comparison.json"
    records = []
    for i in range(24):
        opened = START + timedelta(minutes=15 * i)
        decision = opened + timedelta(seconds=last)
        records.append({
            "sample_id": str(i), "market_id": f"ticker-{i}", "target": target,
            "opens_at": opened.isoformat(), "decision_at": decision.isoformat(),
            "label_observed_at": (opened + timedelta(seconds=label_delay + 1)).isoformat(),
            "label_origin": source, "label": i % 2, "baseline_probability": .95,
            "baseline_generated_at": (opened + timedelta(seconds=first)).isoformat(),
            "baseline_model_version": "version-a", "baseline_call": "WAIT",
            "families": {family: {"received_at": decision.isoformat(), "event_at": decision.isoformat(),
                                  "quality": "VERIFIED", "values": {"v": i % 2}}
                         for family in ("price", "walls", "trades")},
        })
    records[-1]["families"]["trades"]["quality"] = "STALE"
    spec.write_text(json.dumps({
        "target": target, "competition": True, "threshold": .55, "min_train": 20, "l2": 10,
        "eval_start": records[21]["decision_at"], "eval_end": (START + timedelta(hours=7)).isoformat(),
        "flow_max_age_s": 2, "candidates": {"price_only": ["price.v"], "price_walls": ["price.v", "walls.v"],
                                           "price_walls_trades": ["price.v", "walls.v", "trades.v"]},
    }))
    export.write_text("\n".join(map(json.dumps, records)))
    report = research.evaluate(export, spec)
    assert report["target"] == target and report["scoreboard_schema"] == "order-flow-competition-v1"
    assert report["approaches"]["recorded_baseline"]["wait"] == 2
    assert report["approaches"]["price_walls"]["missing_from_evaluation"] == 1
    assert report["approaches"]["combined_research"]["versions"] == ["equal-weight-abc-v1"]
    assert report["approaches"]["combined_research"]["n"] == report["approaches"]["price_only"]["n"]
    assert "trades_increment" in report["comparisons"]
    assert set(report["comparisons_to_recorded_baseline"]) == {
        "price_only", "price_walls", "price_walls_trades", "combined_research"}
    assert (report["comparisons"]["price_walls"]["vs_recorded_baseline_ci95"]
            == report["comparisons_to_recorded_baseline"]["price_walls"]["paired_bootstrap_ci95"])
    records[-1]["decision_at"] = (datetime.fromisoformat(records[-1]["decision_at"]) + timedelta(seconds=1)).isoformat()
    export.write_text("\n".join(map(json.dumps, records)))
    with pytest.raises(ValueError, match="decision outside target cutoff"):
        research.evaluate(export, spec)


def test_calibration_bins_do_not_drop_decimal_boundary_probabilities():
    metrics = research._metrics([.3, .6, .7, .8, .9, 1], [0, 1, 1, 0, 1, 1], .55)
    assert sum(row["n"] for row in metrics["calibration_bins"]) == 6

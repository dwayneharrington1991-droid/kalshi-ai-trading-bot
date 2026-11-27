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

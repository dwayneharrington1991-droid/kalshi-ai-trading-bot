"""L13 feature alignment (candidate only): built from inputs, monotone, safe on absent values, frozen test v4."""
import importlib.util
import json
import random
from datetime import timedelta
from pathlib import Path

import pytest

from src.signals_display import feature_alignment as fa

ROOT = Path(__file__).resolve().parents[1]


def _rows(name, values):
    return [{name: v} for v in values]


def test_identical_distributions_map_to_themselves():
    rng = random.Random(1)
    vals = [rng.gauss(0, 1) for _ in range(500)]
    al = fa.build(_rows("x", vals), _rows("x", vals), ["x"])
    for v in (-1.3, 0.0, 0.4, 2.1):
        assert abs(fa.apply({"x": v}, al)["x"] - v) < 0.05


def test_shifted_distribution_is_mapped_back_and_order_is_kept():
    rng = random.Random(2)
    ref = [rng.gauss(0, 1) for _ in range(800)]
    new = [rng.gauss(5, 2) for _ in range(800)]               # what a changed computation produces
    al = fa.build(_rows("x", ref), _rows("x", new), ["x"])
    mapped = [fa.apply({"x": v}, al)["x"] for v in sorted(new)]
    assert abs(sum(mapped) / len(mapped)) < 0.1
    assert all(b >= a for a, b in zip(mapped, mapped[1:]))     # monotone
    assert fa.apply({"x": 99.0}, al)["x"] == al["maps"]["x"]["reference"][-1]   # clipped to reference range


def test_absent_and_unmapped_inputs_are_untouched():
    rng = random.Random(3)
    al = fa.build(_rows("x", [rng.random() for _ in range(100)]), _rows("x", [rng.random() for _ in range(100)]), ["x"])
    assert fa.apply({"y": 1.5}, al) == {"y": 1.5}
    assert fa.apply({"x": None, "y": 2.0}, al) == {"x": None, "y": 2.0}


def test_too_few_values_are_not_mapped_and_format_is_checked():
    al = fa.build(_rows("x", [1.0] * 10), _rows("x", [2.0] * 10), ["x"])
    assert al["maps"] == {}
    with pytest.raises(ValueError):
        fa.apply({"x": 1.0}, {"format": "other", "maps": {}})


def test_builder_never_reads_outcomes():
    src = (ROOT / "scripts/build_feature_alignment.py").read_text(encoding="utf-8")
    assert "forecast_outcomes\"]" not in src and "forecast_outcomes']" not in src


def test_frozen_alignment_covers_the_skewed_inputs_and_predates_the_v4_freeze():
    al = json.loads((ROOT / "research_artifacts/feature_alignment_20261122.json").read_text())
    assert len(al["maps"]) == 30 and al["built_from"].startswith("input values only")
    spec = importlib.util.spec_from_file_location("fe4", ROOT / "scripts/forward_eval_v4.py")
    fe4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fe4)
    assert al["recent_window"][1] <= fe4.FREEZE_V4.isoformat()


def test_v4_counts_only_openings_after_the_freeze(tmp_path):
    spec = importlib.util.spec_from_file_location("fe4", ROOT / "scripts/forward_eval_v4.py")
    fe4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fe4)
    names = ["distance_scaled", "micro_30s_trade_flow_imbalance"]
    prep = {"names": names, "means": [0, 0], "scales": [1, 1]}
    art = {"model": {"kind": "opening_ensemble", "mixture_weights": [0.25] * 4, "components": [
        {"kind": "logistic", "preprocessor": prep, "weights": [0.3, 0.5, 0.1, 0.1, 0.0], "calibration": [1, 0]},
        {"kind": "ridge", "preprocessor": prep, "weights": [0.05, 0, 0, 0, 0.5], "calibration": [1, 0]},
        {"kind": "opening_gradient_boosting", "preprocessor": prep, "intercept": 0.0, "calibration": [1, 0],
         "stumps": [{"feature": 0, "threshold": 0.0, "left": -0.2, "right": 0.2}]},
        {"kind": "volatility_baseline"}]}}
    rng = random.Random(4)
    ref = [{"micro_30s_trade_flow_imbalance": rng.gauss(0, 1)} for _ in range(100)]
    new = [{"micro_30s_trade_flow_imbalance": rng.gauss(3, 1)} for _ in range(100)]
    al_path = tmp_path / "al.json"
    al_path.write_text(json.dumps(fa.build(ref, new, ["micro_30s_trade_flow_imbalance"])))
    rows, outs = [], []
    start = fe4.FREEZE_V4 - timedelta(hours=2)
    for i in range(16):
        o = start + timedelta(minutes=15 * i)
        f = {"distance_scaled": rng.gauss(0, 1), "micro_30s_trade_flow_imbalance": rng.gauss(3, 1)}
        p = {"opens_at": o.isoformat(), "model": "opening-candidate:x", "frozen_features": {"values": f}}
        rows.append(["M", f"T{i}", o.isoformat(), "", "", 0.5, "ON_TIME", json.dumps(p)])
        outs.append([f"T{i}", "official", "", i % 2])
    j = tmp_path / "j.json"
    j.write_text(json.dumps({"btc_engine_opening": {"rows": rows}, "forecast_outcomes": {"rows": outs}}))
    a = tmp_path / "a.json"
    a.write_text(json.dumps(art))
    rep = fe4.run(str(j), str(a), str(al_path))
    assert rep["forward_openings"] == 8 and rep["decision"] == "NOT ENOUGH FORWARD DATA"

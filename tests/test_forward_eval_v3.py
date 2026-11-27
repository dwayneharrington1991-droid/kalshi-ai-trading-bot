"""Forward test v3: freeze exclusion, exact live reproduction, and masking only the skewed inputs."""
import importlib.util
import json
import random
from datetime import timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fe3", ROOT / "scripts/forward_eval_v3.py")
fe3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fe3)

NAMES = ["distance_scaled", "return_60m", "micro_30s_trade_flow_imbalance", "advanced_trade_intensity"]
PREP = {"names": NAMES, "means": [0.0] * 4, "scales": [1.0] * 4}
K = len(NAMES)
ARTIFACT = {"model": {"kind": "opening_ensemble", "mixture_weights": [0.25, 0.25, 0.25, 0.25], "components": [
    {"kind": "logistic", "preprocessor": PREP, "weights": [0.3, -0.2, 0.5, 0.4] + [0.1] * K + [0.0],
     "calibration": [1.0, 0.0]},
    {"kind": "ridge", "preprocessor": PREP, "weights": [0.05, -0.03, 0.0, 0.0] + [0.0] * K + [0.5],
     "calibration": [1.0, 0.0]},
    {"kind": "opening_gradient_boosting", "preprocessor": PREP, "intercept": 0.0, "calibration": [1.0, 0.0],
     "stumps": [{"feature": 0, "threshold": 0.0, "left": -0.2, "right": 0.2}]},
    {"kind": "volatility_baseline"}]}}


def _live(features, baseline):
    from src.signals_display.opening_models import predict
    return predict(ARTIFACT["model"], features, baseline_probability=baseline)


def test_only_micro_and_flow_inputs_are_masked():
    assert fe3.skewed_inputs(ARTIFACT) == ["micro_30s_trade_flow_imbalance", "advanced_trade_intensity"]


def test_baseline_recovery_reproduces_live_when_nothing_is_skewed():
    f = {"distance_scaled": 0.7, "return_60m": -1.2}          # no skewed inputs present
    live = _live(f, 0.62)
    c = fe3.candidate_probabilities(ARTIFACT, f, live)
    assert abs(c["masked"] - live) < 1e-12


def test_masking_changes_only_the_skewed_contribution():
    f = {"distance_scaled": 0.7, "return_60m": -1.2, "micro_30s_trade_flow_imbalance": 4.0,
         "advanced_trade_intensity": 5.0}
    live = _live(f, 0.4)
    expected = _live({"distance_scaled": 0.7, "return_60m": -1.2}, 0.4)
    assert abs(fe3.candidate_probabilities(ARTIFACT, f, live)["masked"] - expected) < 1e-12


def test_openings_before_the_freeze_never_count(tmp_path):
    rng = random.Random(3)
    rows, outs = [], []
    start = fe3.FREEZE_V3 - timedelta(hours=5)
    for i in range(40):
        o = start + timedelta(minutes=15 * i)
        f = {n: rng.gauss(0, 1) for n in NAMES}
        t = f"T{i}"
        payload = {"opens_at": o.isoformat(), "model": "opening-candidate:x", "frozen_features": {"values": f}}
        rows.append(["PUBLIC MARKET DATA", t, o.isoformat(), o.isoformat(), o.isoformat(), _live(f, 0.5), "ON_TIME",
                     json.dumps(payload)])
        outs.append([t, "official", o.isoformat(), rng.randint(0, 1)])
    j = tmp_path / "j.json"
    j.write_text(json.dumps({"btc_engine_opening": {"rows": rows}, "forecast_outcomes": {"rows": outs}}))
    a = tmp_path / "a.json"
    a.write_text(json.dumps(ARTIFACT))
    rep = fe3.run(str(j), str(a))
    assert rep["forward_openings"] == 20                       # 20 of 40 openings are at/after the freeze
    assert rep["decision"] == "NOT ENOUGH FORWARD DATA"
    assert all(np.isfinite(c["brier"]) for c in rep["candidates"].values())

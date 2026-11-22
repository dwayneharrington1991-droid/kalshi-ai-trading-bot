"""Second AI call: computed from the immutable opening record, reproducibility-checked, never the live call."""
import json
from pathlib import Path

from src.signals_display import feature_alignment as fa
from src.signals_display.challenger_view import ChallengerView

ROOT = Path(__file__).resolve().parents[1]
NAMES = ["distance_scaled", "micro_30s_trade_flow_imbalance"]
PREP = {"names": NAMES, "means": [0, 0], "scales": [1, 1]}
ART = {"model": {"kind": "opening_ensemble", "mixture_weights": [0.25] * 4, "components": [
    {"kind": "logistic", "preprocessor": PREP, "weights": [0.3, 0.9, 0.1, 0.1, 0.0], "calibration": [1, 0]},
    {"kind": "ridge", "preprocessor": PREP, "weights": [0.05, 0, 0, 0, 0.5], "calibration": [1, 0]},
    {"kind": "opening_gradient_boosting", "preprocessor": PREP, "intercept": 0.0, "calibration": [1, 0],
     "stumps": [{"feature": 0, "threshold": 0.0, "left": -0.2, "right": 0.2}]},
    {"kind": "volatility_baseline"}]}}


def _alignment(tmp_path):
    ref = [{"micro_30s_trade_flow_imbalance": x / 50 - 1} for x in range(100)]
    new = [{"micro_30s_trade_flow_imbalance": x / 10 + 3} for x in range(100)]
    p = tmp_path / "al.json"
    p.write_text(json.dumps(fa.build(ref, new, ["micro_30s_trade_flow_imbalance"])))
    return p


def _opening(values, base=0.5):
    from src.signals_display.opening_models import predict
    return {"ticker": "KX-1", "generated_at": "g", "closes_at": "2099-01-01T00:00:00+00:00",
            "frozen_features": {"values": values}, "baseline_probability_up": base,
            "up": predict(ART["model"], values, baseline_probability=base)}


def test_challenger_uses_the_same_frozen_inputs_with_aligned_values(tmp_path):
    view = ChallengerView(ART, _alignment(tmp_path))
    r = view.call(_opening({"distance_scaled": 0.2, "micro_30s_trade_flow_imbalance": 8.0}))
    assert r["status"] == "READY" and r["up"] < r["live_up"]          # skewed input pulled back
    assert abs(r["combined_up"] - (r["up"] + r["live_up"]) / 2) < 1e-12 and r["combined"] in ("UP", "DOWN", "WAIT")
    assert r["direction"] in {"UP", "DOWN", "WAIT"} and "NOT VALIDATED" in r["label"]


def test_unreproducible_live_probability_is_not_shown(tmp_path):
    view = ChallengerView(ART, _alignment(tmp_path))
    o = _opening({"distance_scaled": 0.2})
    o["up"] = 0.99
    assert view.call(o)["status"] == "UNAVAILABLE"


def test_no_record_or_no_artifact_means_no_call(tmp_path):
    assert ChallengerView(ART, _alignment(tmp_path)).call({})["status"] == "WAITING"
    assert ChallengerView(None, _alignment(tmp_path)).call(_opening({"distance_scaled": 0.1}))["status"] == "UNAVAILABLE"


def test_value_is_fixed_per_market_record(tmp_path):
    view = ChallengerView(ART, _alignment(tmp_path))
    o = _opening({"distance_scaled": 0.2, "micro_30s_trade_flow_imbalance": 8.0})
    first = view.call(o)
    view.alignment["maps"] = {}                                      # later changes cannot rewrite it
    assert view.call(o) == first


def test_dashboard_shows_both_calls_and_labels_the_challenger():
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    sec = html[html.index('id="minimal-opening"'):]
    sec = sec[:sec.index("</section>")]
    assert 'id="minimal-opening-card"' in sec and 'id="challenger-card"' in sec
    start = html.index("Challenger AI card: reads only /api/challenger-opening")
    block = html[start:html.index("</script>", start)]
    assert "innerHTML" not in block and "minimal-opening-call" not in block
    assert "NOT VALIDATED" in sec and "official call" in sec
    assert 'route == "/api/challenger-opening"' in (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")

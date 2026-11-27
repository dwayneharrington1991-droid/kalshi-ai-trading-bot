"""The order-flow scoring gate stays on the production probability unless every approval/freshness gate passes."""
import hashlib
import json

from src.signals_display import orderflow_gate as g


def _model(tmp_path):
    m = tmp_path / "m.json"
    m.write_text(json.dumps({"intercept": 0.0, "baseline_weight": 1.0, "features": ["book.x"], "weights": [1.0],
                             "mean": [0.0], "scale": [1.0]}))
    return m, hashlib.sha256(m.read_bytes()).hexdigest()


def test_disabled_by_default(tmp_path):
    r = g.enhanced(0.6, {"book.x": 2.0}, 100.0, 101.0, flag=tmp_path / "none")
    assert r["p_up"] == 0.6 and r["source"] == "production" and "disabled" in r["reason"]


def test_all_gates(tmp_path):
    m, sha = _model(tmp_path)
    flag = tmp_path / "flag"
    flag.write_text("0" * 64)
    assert "hash" in g.enhanced(0.6, {"book.x": 2.0}, 100.0, 101.0, m, flag)["reason"]
    flag.write_text(sha)
    assert "stale" in g.enhanced(0.6, {"book.x": 2.0}, 90.0, 101.0, m, flag)["reason"]
    assert "stale" in g.enhanced(0.6, {"book.x": 2.0}, 102.0, 101.0, m, flag)["reason"]     # future snapshot
    assert "invalid feature" in g.enhanced(0.6, {"book.x": float("nan")}, 100.0, 101.0, m, flag)["reason"]
    r = g.enhanced(0.5, {"book.x": 2.0}, 100.0, 101.0, m, flag)
    assert r["source"] == "orderflow" and abs(r["p_up"] - 1 / (1 + 2.718281828459045 ** -2)) < 1e-9
    m.write_text(m.read_text() + " ")                                                      # weights changed
    assert g.enhanced(0.5, {"book.x": 2.0}, 100.0, 101.0, m, flag)["source"] == "production"

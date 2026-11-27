"""Consensus scorecard: WAIT is coverage not accuracy; unsettled markets excluded; break-even includes costs."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("sc", Path(__file__).resolve().parents[1] / "scripts" / "score_consensus.py")
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def test_scorecard():
    rows = [("a", 0.60, 0.58, 0.59, "UP", 1), ("b", 0.60, 0.50, 0.55, "WAIT", 0),
            ("c", 0.40, 0.40, 0.40, "DOWN", 1), ("d", 0.60, 0.60, 0.60, "UP", None)]
    r = sc.score(rows)
    assert r["settled_markets"] == 3
    k = r["consensus"]
    assert (k["calls"], k["correct"], k["wait"]) == (2, 1, 1) and abs(k["coverage"] - 2 / 3) < 1e-12
    assert r["live"]["calls"] == 3 and r["challenger"]["calls"] == 2
    assert abs(r["assumptions"]["break_even_accuracy"] - 0.5275) < 1e-9
    assert abs(k["est_profit_per_contract_usd"] - (0.5 - 0.5275)) < 1e-9

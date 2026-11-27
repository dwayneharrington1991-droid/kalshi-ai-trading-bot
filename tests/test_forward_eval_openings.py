"""Pre-registered forward evaluation: no future outcome reaches a prediction, and the decision rule holds."""
import importlib.util
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("forward_eval", ROOT / "scripts/forward_eval_openings.py")
fe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fe)

T0 = datetime(2026, 11, 17, tzinfo=timezone.utc)
ARTIFACT = {"model": {"components": [{}, {"preprocessor": {"names": ["return_1m"], "means": [0.0], "scales": [1.0]}}]}}


def _export(n, seed=5):
    rng = random.Random(seed)
    opening, outcomes, quotes = [], [], []
    for i in range(n):
        opens = T0 + timedelta(minutes=15 * i)
        d = rng.gauss(0, 1)
        up = 1 if d + rng.gauss(0, 0.7) > 0 else 0
        ticker = f"T{i:04d}"
        payload = {"opens_at": opens.isoformat(), "frozen_features": {"values": {"distance_scaled": d, "return_1m": rng.gauss(0, 1)}}}
        opening.append(["PUBLIC", ticker, opens.isoformat(), opens.isoformat(), None, 0.5, "DELAYED", json.dumps(payload)])
        outcomes.append([ticker, "official", (opens + timedelta(minutes=15)).isoformat(), up])
        quotes.append([None, ticker, None, None, 2.0, 0.49, 0.5, 0.49, 0.5, 0, 0, 1.0, "quadratic", None, None])
    return {"btc_engine_opening": {"rows": opening}, "forecast_outcomes": {"rows": outcomes},
            "opening_quote_snapshots": {"rows": quotes}}


def test_a_prediction_never_depends_on_outcomes_known_only_later():
    export = _export(160)
    rows = fe.load_rows(export)
    before = fe.predictions(rows, ARTIFACT)
    flipped = json.loads(json.dumps(export))
    for r in flipped["forecast_outcomes"]["rows"][130:]:
        r[3] = 1 - r[3]                           # change every outcome from opening 130 on
    after = fe.predictions(fe.load_rows(flipped), ARTIFACT)
    for name in ("dist", "core16", "base"):
        # Opening i may use only openings at least 30 minutes earlier: 130 and 131 are unaffected.
        assert before[name][:132] == after[name][:132]
        assert before[name][133:] != after[name][133:]


def test_decision_waits_for_the_preregistered_number_of_forward_openings():
    rows = fe.load_rows(_export(260))
    preds = fe.predictions(rows, ARTIFACT)
    early = fe.report(rows, preds, freeze=rows[150]["opens"].isoformat())
    assert early["forward_openings"] == 110 and early["decision"] == "NOT ENOUGH FORWARD DATA"
    assert early["passed_preregistered_rule"] == []


def test_a_truly_predictive_candidate_can_pass_and_a_useless_one_cannot():
    rows = fe.load_rows(_export(520))
    preds = fe.predictions(rows, ARTIFACT)
    out = fe.report(rows, preds, freeze=rows[110]["opens"].isoformat())
    assert out["forward_openings"] >= fe.MIN_FORWARD
    assert "dist" in out["passed_preregistered_rule"]      # synthetic outcomes follow distance_scaled
    assert "core16" not in out["passed_preregistered_rule"]  # return_1m is pure noise here
    assert out["decision"] == "ELIGIBLE FOR A SEPARATE PROMOTION REVIEW"


def test_frozen_definitions_are_unchanged():
    assert fe.FREEZE == "2026-11-20T06:15:00+00:00" and fe.MIN_FORWARD == 200
    assert fe.CANDIDATES == {"dist": {"inputs": "distance_scaled", "ridge": 1.0},
                             "core16": {"inputs": "live price_core preprocessor (16)", "ridge": 20.0}}

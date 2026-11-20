"""Research round v2: frozen-period exclusion and no use of outcomes unknown at each opening."""
import importlib.util
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("rv", ROOT / "scripts/research_opening_v2.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)

NAMES = ["distance_scaled", "return_60m", "return_15m", "volatility_24h"]
PREP = {"names": NAMES, "means": [0.0] * 4, "scales": [1.0] * 4}
ARTIFACT = {"model": {"components": [{"preprocessor": PREP}, {"preprocessor": PREP}]}}


def _export(path, start, n, seed=1, flip_after=None):
    rng = random.Random(seed)
    opening, outcomes = [], []
    for i in range(n):
        o = start + timedelta(minutes=15 * i)
        v = {k: rng.gauss(0, 1) for k in NAMES}
        y = 1 if v["distance_scaled"] + rng.gauss(0, 1) > 0 else 0
        if flip_after is not None and i >= flip_after:
            y = 1 - y
        t = f"T{start:%m%d}{i:04d}"
        payload = {"opens_at": o.isoformat(), "model": "opening-candidate/x", "frozen_features": {"values": v}}
        opening.append(["P", t, o.isoformat(), o.isoformat(), None, 0.5, "DELAYED", json.dumps(payload)])
        outcomes.append([t, "official", o.isoformat(), y])
    Path(path).write_text(json.dumps({"btc_engine_opening": {"rows": opening}, "forecast_outcomes": {"rows": outcomes},
                                      "opening_quote_snapshots": {"rows": []}}))


def test_openings_at_or_after_the_frozen_test_are_never_loaded(tmp_path):
    _export(tmp_path / "a.json", rv.FREEZE - timedelta(hours=10), 80)
    rows = rv.load([tmp_path / "a.json"])
    assert rows and max(r["opens"] for r in rows) < rv.FREEZE


def test_walk_forward_predictions_never_use_later_outcomes(tmp_path, monkeypatch):
    start = datetime(2026, 11, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(rv, "FREEZE", datetime(2100, 1, 1, tzinfo=timezone.utc))
    _export(tmp_path / "a.json", start, 260)
    _export(tmp_path / "b.json", start, 260, flip_after=230)        # change outcomes from opening 230 on
    eval_start = start + timedelta(minutes=15 * 200)
    outs = []
    for name in ("a.json", "b.json"):
        rows = rv.load([tmp_path / name])
        idx, P, _t, _k = rv.walk_forward(rows, rv.Design(rows, ARTIFACT), eval_start=eval_start)
        outs.append((idx, P))
    (ia, pa), (ib, pb) = outs
    first_affected = 232                                             # 30-minute gap: opening i uses outcomes up to i-2
    for name in ("dist", "core16_ridge", "ensemble"):
        early = [i for i in ia if i < first_affected]
        np.testing.assert_allclose(pa[name][early], pb[name][early])

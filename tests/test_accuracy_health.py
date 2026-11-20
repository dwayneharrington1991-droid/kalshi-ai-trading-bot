"""Read-only accuracy health monitor: technical data health and predictive edge are judged separately."""
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import accuracy_health as ah

NOW = datetime(2026, 11, 20, 19, 20, tzinfo=timezone.utc)
NAMES = [f"x{i}" for i in range(40)]
ARTIFACT = {"model_id": "m1", "labels_available_through": "2026-11-12T06:30:00+00:00",
            "model": {"components": [{"preprocessor": {"names": NAMES}}]}}


def _journal(tmp_path, *, n=120, skill=0.0, missing=0, delay=2.0, last_pred_age=2.0, withheld_last=False, seed=4):
    path = tmp_path / "j.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE btc_engine_predictions (recorded_at TEXT)")
    db.execute("CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT, generated_at TEXT, cutoff TEXT, closes_at TEXT, up REAL, status TEXT, payload TEXT)")
    db.execute("CREATE TABLE forecast_outcomes (ticker TEXT, origin TEXT, observed_at TEXT, outcome INTEGER)")
    db.execute("CREATE TABLE opening_quote_snapshots (ticker TEXT, seconds_from_open REAL, up_ask REAL, down_ask REAL)")
    for k in range(300, 0, -1):
        db.execute("INSERT INTO btc_engine_predictions VALUES (?)", ((NOW - timedelta(seconds=last_pred_age + 2 * k)).isoformat(),))
    db.execute("INSERT INTO btc_engine_predictions VALUES (?)", ((NOW - timedelta(seconds=last_pred_age)).isoformat(),))
    rng = random.Random(seed)
    first = NOW.replace(minute=15) - timedelta(minutes=15 * (n - 1))
    for i in range(n):
        opens = first + timedelta(minutes=15 * i)
        signal = rng.gauss(0, 1)
        y = 1 if rng.random() < 0.5 + skill * (1 if signal > 0 else -1) else 0
        up = min(0.95, max(0.05, 0.5 + 0.08 * signal))
        last = i == n - 1
        values = {name: (None if j < missing else 0.1) for j, name in enumerate(NAMES)}
        payload = {"opens_at": opens.isoformat(), "model": "opening-candidate/m1",
                   "direction": "UP" if up >= 0.55 else "DOWN" if up <= 0.45 else "WAIT",
                   "opening_delay_seconds": delay, "data_quality": {"status": "SUPPORTED PROXY INPUTS"},
                   "frozen_features": {"values": values}}
        db.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,?,?)",
                   (f"T{i}", (opens + timedelta(seconds=delay)).isoformat(), opens.isoformat(), None,
                    None if (last and withheld_last) else up, "DELAYED", json.dumps(payload)))
        if not last:
            db.execute("INSERT INTO forecast_outcomes VALUES (?, 'official', ?, ?)", (f"T{i}", opens.isoformat(), y))
            db.execute("INSERT INTO opening_quote_snapshots VALUES (?, 2.0, 0.51, 0.51)", (f"T{i}",))
    db.commit()
    db.close()
    return path


def test_fresh_complete_data_is_healthy_but_a_coin_flip_model_has_no_verified_edge(tmp_path):
    out = ah.assess(_journal(tmp_path, skill=0.0), now=NOW, artifact=ARTIFACT)
    assert out["technical_data_health"]["status"] == "HEALTHY"
    assert out["predictive_edge"]["status"] in ("NO VERIFIED EDGE", "DEGRADED")
    assert out["recommendation"].startswith("NO VERIFIED EDGE")


def test_a_strong_model_can_show_supported_edge(tmp_path):
    out = ah.assess(_journal(tmp_path, n=400, skill=0.35), now=NOW, artifact=ARTIFACT)
    assert out["predictive_edge"]["status"] == "EDGE SUPPORTED"


def test_stopped_predictions_are_stale_and_recommend_wait(tmp_path):
    out = ah.assess(_journal(tmp_path, last_pred_age=120), now=NOW, artifact=ARTIFACT)
    assert out["technical_data_health"]["status"] == "STALE"
    assert out["recommendation"].startswith("WAIT")


@pytest.mark.parametrize("kwargs,reason", [
    ({"missing": 35}, "missing 35 model inputs"),
    ({"delay": 25.0}, "after the open"),
    ({"withheld_last": True}, "withheld"),
])
def test_degraded_inputs_recommend_wait(tmp_path, kwargs, reason):
    out = ah.assess(_journal(tmp_path, **kwargs), now=NOW, artifact=ARTIFACT)
    tech = out["technical_data_health"]
    assert tech["status"] == "DEGRADED" and any(reason in r for r in tech["reasons"])
    assert out["recommendation"].startswith("WAIT")


def test_routine_micro_input_absence_alone_is_not_degraded(tmp_path):
    out = ah.assess(_journal(tmp_path, missing=28), now=NOW, artifact=ARTIFACT)
    assert out["technical_data_health"]["status"] == "HEALTHY"
    assert out["technical_data_health"]["share_recent_openings_missing_10_plus_inputs"] == 1.0


def test_too_few_settled_openings_is_insufficient_evidence(tmp_path):
    out = ah.assess(_journal(tmp_path, n=30), now=NOW, artifact=ARTIFACT)
    assert out["predictive_edge"]["status"] == "INSUFFICIENT EVIDENCE"


def test_the_journal_is_never_modified(tmp_path):
    path = _journal(tmp_path)
    before = path.read_bytes()
    ah.assess(path, now=NOW, artifact=ARTIFACT)
    assert path.read_bytes() == before


def test_calibration_slope_detects_underconfidence():
    rng = random.Random(1)
    ps, ys = [], []
    for _ in range(4000):
        true = rng.uniform(0.2, 0.8)
        ps.append(0.5 + (true - 0.5) / 2)                  # probabilities squeezed toward 0.5
        ys.append(1 if rng.random() < true else 0)
    assert 1.7 < ah._calibration_slope(ps, ys) < 2.5

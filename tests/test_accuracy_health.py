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
            db.execute("INSERT INTO opening_quote_snapshots VALUES (?, -2.0, 0.51, 0.51)", (f"T{i}",))   # pre-open quote
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


def test_both_scoreboard_populations_are_reported_with_exclusions(tmp_path):
    path = _journal(tmp_path)
    db = sqlite3.connect(path)
    # Two directional records whose confidence was not displayable (like the 2026-11-16 fallback calls).
    for i, (d, y) in enumerate((("UP", 0), ("DOWN", 1))):
        payload = {"opens_at": "2026-11-16T18:00:00+00:00", "model": "opening24h-zero-drift-final-average-v1",
                   "direction": d, "confidence": {"status": "EXPERIMENTAL"}}
        db.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,?,?)",
                   (f"F{i}", f"2026-11-16T18:0{i}:00+00:00", None, None, 0.51 if d == "UP" else 0.49, "DELAYED", json.dumps(payload)))
        db.execute("INSERT INTO forecast_outcomes VALUES (?, 'official', 'x', ?)", (f"F{i}", y))
    db.execute("UPDATE btc_engine_opening SET payload=json_set(payload,'$.confidence.status','EXPERIMENTAL CALIBRATED CANDIDATE') WHERE ticker LIKE 'T%'")
    db.commit()
    db.close()
    boards = ah.assess(path, now=NOW, artifact=ARTIFACT)["scoreboards"]
    dash, every = boards["dashboard_scoreboard"], boards["all_directional_calls"]
    assert every["calls"] == dash["calls"] + 2
    assert every["correct"] == dash["correct"]                     # both excluded calls were wrong
    (reason, entry), = boards["excluded_from_dashboard_scoreboard"].items()
    assert entry == {"calls": 2, "correct": 0} and "EXPERIMENTAL" in reason


def test_input_drift_flags_inputs_far_from_training(tmp_path):
    art = json.loads(json.dumps(ARTIFACT))
    art["model"]["components"][0]["preprocessor"].update(means=[0.1] * 40, scales=[1.0] * 40)
    assert ah.assess(_journal(tmp_path), now=NOW, artifact=art)["predictive_edge"]["input_drift"]["status"] == "WITHIN TRAINING RANGE"
    art["model"]["components"][0]["preprocessor"].update(means=[5.0] * 40)
    other = tmp_path / "drifted"
    other.mkdir()
    assert ah.assess(_journal(other), now=NOW, artifact=art)["predictive_edge"]["input_drift"]["status"] == "DRIFTED"


def test_cached_report_recomputes_at_most_once_per_minute(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ah, "assess", lambda *a, **k: calls.append(1) or {"ok": len(calls)})
    clock = [NOW]
    cache = ah.CachedReport(tmp_path / "j.db", clock=lambda: clock[0])
    cache.get(); cache.get()
    clock[0] = NOW + timedelta(seconds=61)
    assert cache.get() == {"ok": 2} and len(calls) == 2


def test_cached_report_never_raises(tmp_path):
    out = ah.CachedReport(tmp_path / "missing.db").get()
    assert out["status"] == "UNAVAILABLE"


def test_final_minute_coverage_counts_closed_contracts(tmp_path):
    path = _journal(tmp_path)
    db = sqlite3.connect(path)
    db.execute("DROP TABLE btc_engine_predictions")
    db.execute("CREATE TABLE btc_engine_predictions (ticker TEXT, timestamp TEXT, recorded_at TEXT, closes_at TEXT)")
    for c in range(4):
        close = NOW - timedelta(minutes=15 * (3 - c)) - timedelta(minutes=1)
        for s in range(0, 900, 2):
            at = close - timedelta(seconds=900 - s)
            if c < 2 and (close - at).total_seconds() < 60:
                continue                                   # the first two contracts lost their final minute
            db.execute("INSERT INTO btc_engine_predictions VALUES (?,?,?,?)", (f"C{c}", at.isoformat(), at.isoformat(), close.isoformat()))
    db.commit()
    db.close()
    fm = ah._final_minute(sqlite3.connect(path), NOW)
    assert fm == {"contracts": 4, "with_final_minute_prediction": 2, "share": 0.5}



def test_opening_quotes_are_only_those_available_at_or_before_the_open():
    import sqlite3
    from src.signals_display.accuracy_health import _pre_open_quotes
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE opening_quote_snapshots (ticker TEXT, seconds_from_open REAL, up_ask REAL, down_ask REAL, "
               "quote_event_time TEXT, opens_at TEXT)")
    o = "2026-11-23T03:00:00+00:00"
    rows = [("PRE", -3.0, 0.50, 0.52, "2026-11-23T02:59:57+00:00", o), ("PRE", -1.0, 0.55, 0.47, "2026-11-23T02:59:59+00:00", o),
            ("PRE", 2.0, 0.90, 0.12, "2026-11-23T03:00:02+00:00", o),                       # late: never used
            ("EDGE", 0.0, 0.60, 0.42, o, o),                                              # exactly at the open: allowed
            ("LATE", 4.0, 0.70, 0.32, "2026-11-23T03:00:04+00:00", o),                    # only a late quote: missing
            ("SKEW", -0.5, 0.61, 0.41, "2026-11-23T03:00:01+00:00", o),                   # received early, stamped late
            ("NOEVT", -2.0, 0.58, 0.44, None, o),                                         # no exchange time: receipt rule
            ("OLD", -9.0, 0.50, 0.52, "2026-11-23T02:59:51+00:00", o)]                    # outside the 5 s window
    for r in reversed(rows):                                                              # out-of-order inserts
        db.execute("INSERT INTO opening_quote_snapshots VALUES (?,?,?,?,?,?)", r)
    q = _pre_open_quotes(db)
    assert q["PRE"] == (-1.0, 0.55, 0.47) and q["EDGE"] == (0.0, 0.60, 0.42) and q["NOEVT"] == (-2.0, 0.58, 0.44)
    assert "LATE" not in q and "SKEW" not in q and "OLD" not in q


def test_model_id_match_is_exact_not_substring():
    src = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/signals_display/accuracy_health.py").read_text()
    assert 'model.rsplit("/", 1)[-1] != artifact_model_id' in src and "artifact_model_id not in model" not in src

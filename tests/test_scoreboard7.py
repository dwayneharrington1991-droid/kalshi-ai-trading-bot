"""Seven-day scoreboard: counts, window boundaries, missing vs WAIT, duplicates, delayed settlements, persistence."""
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import scoreboard7 as sb  # noqa: E402

START = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    db = sqlite3.connect(tmp_path / "s.db")
    db.executescript("""
        CREATE TABLE openings(ticker TEXT PRIMARY KEY, opens_at TEXT, closes_at TEXT, generated_at TEXT, claimed_at TEXT,
            kind TEXT, live TEXT, live_up REAL, challenger TEXT, challenger_up REAL, combined TEXT, combined_up REAL, text TEXT);
        CREATE TABLE minute6(ticker TEXT PRIMARY KEY, opens_at TEXT, closes_at TEXT, locked_at TEXT, cutoff TEXT,
            threshold REAL, price REAL, direction TEXT, components_agree INTEGER, reason TEXT, a_p_up REAL, a_dir TEXT,
            b_score REAL, b_dir TEXT, c_p_up REAL, c_dir TEXT, up_ask REAL, down_ask REAL, up_bid REAL, down_bid REAL, payload TEXT);""")
    sb.ensure_tables(db)
    return db


def _open(db, t, opens, live, ch, comb, kind="READY", gen_delay=3):
    g = opens + timedelta(seconds=gen_delay)
    db.execute("INSERT INTO openings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (t, opens.isoformat(), (opens + timedelta(minutes=15)).isoformat(), g.isoformat(), g.isoformat(), kind,
                live, 0.6, ch, 0.6, comb, 0.6, ""))


def _m6(db, t, opens, direction, eligible=3):
    lk = opens + timedelta(seconds=366)
    db.execute("INSERT INTO minute6 (ticker, opens_at, locked_at, direction, payload) VALUES (?,?,?,?,?)",
               (t, opens.isoformat(), lk.isoformat(), direction, json.dumps({"eligible": eligible})))


def _mk(t, opens, outcome, status="VERIFIED_OFFICIAL"):
    return {"ticker": t, "opening_at": opens.isoformat(), "final_outcome": outcome, "outcome_status": status}


def test_starts_at_zero_and_window_is_written_once(tmp_path):
    w = sb.start_window(tmp_path / "w.json", now=START, model="b50b9dc30f6d664c")
    assert w["end"] == (START + timedelta(hours=168)).isoformat()
    again = sb.start_window(tmp_path / "w.json", now=START + timedelta(days=2))
    assert again == w                                                     # never overwritten
    r = sb.compute(_db(tmp_path), w, now=START)
    for m in sb.MODELS:
        assert r["models"][m] == {"correct": 0, "wrong": 0, "wait": 0, "missing": 0, "accuracy": None,
                                  "eligible": 0, "coverage": None}


def test_counts_boundaries_missing_and_delayed_settlements(tmp_path):
    db, w = _db(tmp_path), sb.start_window(tmp_path / "w.json", now=START)
    before = START - timedelta(minutes=15)
    a, b, c, d = (START + timedelta(minutes=15 * i) for i in range(4))
    _open(db, "T0", before, "UP", "UP", "UP")                              # locked before the window: excluded
    _open(db, "TA", a, "UP", "UP", "UP");    _m6(db, "TA", a, "DOWN")
    _open(db, "TB", b, "DOWN", "WAIT", "WAIT"); _m6(db, "TB", b, "WAIT")
    _open(db, "TC", c, "UP", None, None, kind="CHALLENGER_UNAVAILABLE"); _m6(db, "TC", c, "WAIT", eligible=1)
    #  TD: no locked records at all (service down) -> MISSING for every model, never WAIT
    late = START + timedelta(hours=168)
    _open(db, "TL", late, "UP", "UP", "UP")                                # after the window: excluded
    sb.record_outcomes(db, [_mk("T0", before, "UP"), _mk("TA", a, "UP"), _mk("TB", b, "UP"),
                            _mk("TD", d, "DOWN"), _mk("TL", late, "UP"), _mk("TC", c, None, "UNVERIFIED")])
    r = sb.compute(db, w, now=START + timedelta(hours=2))
    assert r["settled_markets"] == 3                                       # TA, TB, TD (TC not settled yet)
    assert r["models"]["AI #1"] == {"correct": 1, "wrong": 1, "wait": 0, "missing": 1, "accuracy": 50.0, "eligible": 3, "coverage": 66.7}
    assert r["models"]["COMBINED CALL"] == {"correct": 1, "wrong": 0, "wait": 1, "missing": 1, "accuracy": 100.0, "eligible": 3, "coverage": 33.3}
    assert r["models"]["MINUTE-6 FINAL"] == {"correct": 0, "wrong": 1, "wait": 1, "missing": 1, "accuracy": 0.0, "eligible": 3, "coverage": 33.3}
    sb.record_outcomes(db, [_mk("TC", c, "UP")])                           # delayed settlement arrives later
    r = sb.compute(db, w, now=START + timedelta(hours=3))
    assert r["models"]["AI #1"]["correct"] == 2 and r["models"]["AI #2"]["missing"] == 2
    assert r["models"]["MINUTE-6 FINAL"]["missing"] == 2                         # invalid minute-6 reading is not a WAIT
    assert sb.record_outcomes(db, [_mk("TA", a, "DOWN")]) == 0             # an outcome is never replaced
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE outcomes7 SET outcome='DOWN'")


def test_window_completes_and_keeps_final_results(tmp_path):
    db, w = _db(tmp_path), sb.start_window(tmp_path / "w.json", now=START)
    a = START + timedelta(hours=100)
    _open(db, "TA", a, "UP", "UP", "UP")
    sb.record_outcomes(db, [_mk("TA", a, "UP")])
    r = sb.compute(db, w, now=START + timedelta(days=30))
    assert r["window"]["complete"] is True and r["window"]["elapsed_hours"] == 168
    assert r["models"]["AI #1"]["correct"] == 1


def test_original_top_right_scoreboard_shows_combined_call_seven_day_numbers(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    db, w = _db(tmp_path), sb.start_window(tmp_path / "w.json", now=START)
    a, b = START + timedelta(minutes=15), START + timedelta(minutes=30)
    _open(db, "TA", a, "UP", "UP", "UP"); _open(db, "TB", b, "DOWN", "WAIT", "WAIT")
    sb.record_outcomes(db, [_mk("TA", a, "UP"), _mk("TB", b, "UP")])
    payload = {"scoreboard7": {"status": "RUNNING", **sb.compute(db, w, now=START + timedelta(hours=1))}}
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    assert 'id="score7-box"' not in html                                   # no second scoreboard box
    assert 'id="ai-scoreboard"' in html and "COMBINED CALL · 7-DAY TEST" in html
    start = html.index("async function refreshScoreboard(){")
    fn = html[start:html.index(chr(10) + "}" + chr(10), start) + 3]
    assert "innerHTML" not in fn and "/api/minute6" in fn
    src = ("var out={};function txt(id,v){out[id]=String(v);}"
           "var fetch=function(){return Promise.resolve({ok:true,json:function(){return Promise.resolve(" + json.dumps(payload) + ");}});};"
           + fn + ";refreshScoreboard().then(function(){console.log(JSON.stringify(out));});")
    res = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["score-correct"] == "1" and out["score-wrong"] == "0" and out["score-accuracy"] == "100.0%"
    assert out["score-sample"] == "Since 2026-11-23 00:00 UTC · 2 settled · 1 WAIT"


def test_first6_scored_on_its_own_six_minute_target_with_ties_and_start_time(tmp_path):
    db, w = _db(tmp_path), sb.start_window(tmp_path / "w.json", now=START)
    db.executescript("""CREATE TABLE first6(ticker TEXT PRIMARY KEY, opens_at TEXT, locked_at TEXT, p_up REAL,
        direction TEXT, version TEXT, reference_open REAL);
        CREATE TABLE first6_outcomes(ticker TEXT PRIMARY KEY, opens_at TEXT, reference_open REAL, reference_6m REAL,
        outcome TEXT, recorded_at TEXT);""")
    a, b, c = (START + timedelta(minutes=15 * (i + 1)) for i in range(3))
    for t, o, d, out in (("TA", a, "DOWN", "DOWN"), ("TB", b, "WAIT", "UP"), ("TC", c, "UP", "FLAT")):
        db.execute("INSERT INTO first6 VALUES (?,?,?,?,?,?,?)",
                   (t, o.isoformat(), (o + timedelta(seconds=12)).isoformat(), .4, d, "v0", 100.0))
        db.execute("INSERT INTO first6_outcomes VALUES (?,?,?,?,?,?)", (t, o.isoformat(), 100.0, 99.0, out, o.isoformat()))
    sb.record_outcomes(db, [_mk("TA", a, "UP")])           # final settlement UP: never used for FIRST 6 MIN
    r = sb.compute(db, w, now=START + timedelta(hours=2))
    f = r["models"]["FIRST 6 MIN"]
    assert (f["correct"], f["wrong"], f["wait"], f["eligible"]) == (1, 0, 1, 2)   # FLAT move not scored
    assert r["first6_scoring_start"] == (a + timedelta(seconds=12)).isoformat()

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
        assert r["models"][m] == {"correct": 0, "wrong": 0, "wait": 0, "missing": 0, "accuracy": None}


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
    assert r["models"]["AI #1"] == {"correct": 1, "wrong": 1, "wait": 0, "missing": 1, "accuracy": 50.0}
    assert r["models"]["COMBINED"] == {"correct": 1, "wrong": 0, "wait": 1, "missing": 1, "accuracy": 100.0}
    assert r["models"]["MINUTE-6"] == {"correct": 0, "wrong": 1, "wait": 1, "missing": 1, "accuracy": 0.0}
    sb.record_outcomes(db, [_mk("TC", c, "UP")])                           # delayed settlement arrives later
    r = sb.compute(db, w, now=START + timedelta(hours=3))
    assert r["models"]["AI #1"]["correct"] == 2 and r["models"]["AI #2"]["missing"] == 2
    assert r["models"]["MINUTE-6"]["missing"] == 2                         # invalid minute-6 reading is not a WAIT
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


def test_dashboard_table_renders_four_rows_with_minute6_accuracy(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    db, w = _db(tmp_path), sb.start_window(tmp_path / "w.json", now=START)
    a = START + timedelta(minutes=15)
    _open(db, "TA", a, "UP", "DOWN", "WAIT"); _m6(db, "TA", a, "UP")
    sb.record_outcomes(db, [_mk("TA", a, "UP")])
    payload = {"scoreboard7": {"status": "RUNNING", **sb.compute(db, w, now=START + timedelta(hours=1))}}
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    top = html[html.index('<section id="minimal-opening"'):]
    assert top.index('id="ai-three-lines"') < top.index('id="score7-box"') < top.index('id="minimal-opening-card"')
    start = html.index("Seven-day scoreboard: reads only /api/minute6")
    block = html[html.rindex("<script>", 0, start) + 8:html.index("</script>", start)]
    assert "innerHTML" not in block
    src = ("function El(){this.children=[];this.textContent='';this.className='';}"
           "El.prototype.appendChild=function(c){this.children.push(c);};El.prototype.removeChild=function(c){this.children.splice(this.children.indexOf(c),1);};"
           "Object.defineProperty(El.prototype,'firstChild',{get:function(){return this.children[0]||null;}});"
           "var els={'score7-head':new El(),'score7-body':new El(),'score7-foot':new El()};"
           "var document={getElementById:function(i){return els[i];},createElement:function(){return new El();}};var setInterval=function(){};"
           "var fetch=function(){return Promise.resolve({ok:true,json:function(){return " + json.dumps(payload) + ";}});};"
           + block + ";setTimeout(function(){console.log(JSON.stringify({head:els['score7-head'].textContent,"
           "rows:els['score7-body'].children.map(function(r){return r.children.map(function(c){return c.textContent;});})}));},30);")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["rows"] == [["COMBINED", "0", "0", "N/A", "1"], ["AI #1", "1", "0", "100.0%", "0"],
                         ["AI #2", "0", "1", "0.0%", "0"], ["MINUTE-6", "1", "0", "100.0%", "0"]]
    assert "7-DAY TEST · 2026-11-23 00:00 UTC → 2026-11-30 00:00 UTC" in r["head"] and "1 settled markets" in r["head"]

"""Minute-6 research: no future data, nothing invented, locked once, immutable, separate from the opening calls."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import minute6 as m6

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from minute6_runner import Minute6Runner  # noqa: E402

OPEN = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
O = int(OPEN.timestamp())


def _candles(drift=5.0, upto=O + 360, base=60000.0):
    """Coinbase layout [time, low, high, open, close, volume], newest first; includes a partial current minute."""
    out = []
    for i, t in enumerate(range(O - 3900, upto + 1, 60)):
        c = base + drift * i + (3 if i % 2 else -3)
        out.append([t, c - 1, c + 1, c - drift, c, 1.0])
    return list(reversed(out))


def _state(ticker="KX-M", threshold=60100.0, up=0.9, updated=OPEN + timedelta(seconds=350)):
    return {"prediction_engine": {"ticker": ticker, "closes_at": (OPEN + timedelta(minutes=15)).isoformat(),
                                  "threshold": threshold, "up": up, "updated_at": updated.isoformat()},
            "scalping_monitor": {"ticker": ticker, "sides": {"up": {"ask": 0.91, "bid": 0.89}, "down": {"ask": 0.11, "bid": 0.09}}}}


class Sender:
    def __init__(self):
        self.calls = []

    def __call__(self, channel, cfg, text):
        self.calls.append(text)
        return 200, {"ok": True, "result": {"message_id": 7}}


def test_closes_stop_at_the_candle_closing_at_minute_6():
    cs = _candles()
    closes = m6.candles_to_closes(cs, O)
    assert len(closes) == 71
    assert closes[-1] == [c for c in cs if c[0] == O + 300][0][4]      # the +6:00 partial minute is never used
    assert m6.candles_to_closes([c for c in cs if c[0] != O - 600], O) is None


def test_consensus_needs_agreement_and_never_invents():
    up = {"status": "ELIGIBLE", "direction": "UP", "p_up": 0.9}
    assert m6.consensus(up, {"status": "ELIGIBLE", "direction": "UP", "score": 1.0}, up)["direction"] == "UP"
    assert m6.consensus(up, {"status": "ELIGIBLE", "direction": "WAIT", "score": 0.25}, up)["direction"] == "WAIT"
    r = m6.consensus(up, {"status": "INELIGIBLE"}, {"status": "INELIGIBLE"})
    assert r["direction"] == "WAIT" and r["eligible"] == 1


def test_runner_locks_once_sends_once_and_is_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    s = Sender()
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), s, {}, candles=_candles,
                      history=lambda: {"markets": [{"ticker": "KX-M", "final_outcome": "UP", "outcome_status": "VERIFIED_OFFICIAL"}]}, log=lambda x: None)
    assert r.step(_state(), now=OPEN + timedelta(seconds=300), now_mono=0) == "OUTSIDE WINDOW"
    assert r.step(_state(), now=OPEN + timedelta(seconds=366), now_mono=1) == "SENT"
    assert r.step(_state(), now=OPEN + timedelta(seconds=380), now_mono=2) == "ALREADY LOCKED"
    assert len(s.calls) == 1 and "MINUTE-6 FINAL: " in s.calls[0] and "KX-M" in s.calls[0]
    assert "AI #1: WAIT 🟡 — N/A" in s.calls[0]                 # no opening row stored for KX-M: nothing invented
    with pytest.raises(sqlite3.DatabaseError):
        r.db.execute("UPDATE minute6 SET direction='DOWN'")
    r.board = (0.0, None)                                  # force a scoreboard refresh
    r.write_latest(_state(), OPEN + timedelta(seconds=390), 500)
    out = json.loads((tmp_path / "m6.json").read_text())
    assert out["status"] == "LOCKED" and out["analytics"]["status"] == "READY"
    assert out["analytics"]["measured"]["side_now"] in ("ABOVE", "BELOW")
    assert out["scoreboard"]["settled"] == 1


def test_late_service_never_backfills(tmp_path, monkeypatch):
    s = Sender()
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), s, {}, candles=_candles, log=lambda x: None)
    assert r.step(_state(), now=OPEN + timedelta(seconds=500), now_mono=0) == "OUTSIDE WINDOW"
    assert not s.calls and "NO READING" in json.loads((tmp_path / "m6.json").read_text())["status"]


def test_engine_published_after_cutoff_is_ineligible(tmp_path, monkeypatch):
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {}, candles=_candles, log=lambda x: None)
    now = OPEN + timedelta(seconds=366)
    r.step(_state(updated=now + timedelta(seconds=5)), now=now, now_mono=0)
    assert r.db.execute("SELECT c_dir FROM minute6").fetchone()[0] is None


def test_parity_with_development_features():
    path = ROOT.parent / "research" / "m6_dev.json"
    if not path.exists():
        pytest.skip("development extract not present")
    sys.path.insert(0, str(ROOT.parent / "research"))
    rows = json.loads(path.read_text())[:25]
    import math
    import statistics as st
    for r in rows:
        c = r["closes"]
        rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - 60, len(c))]
        dev_pa = 0.5 * (1 + math.erf(math.log(c[-1] / r["threshold"]) / (st.pstdev(rets) * 3) / math.sqrt(2)))
        assert abs(m6.component_a(c, r["threshold"])["p_up"] - dev_pa) < 1e-12


def test_api_body_reports_missing_and_stale_output(tmp_path):
    assert json.loads(m6.latest_body(tmp_path / "none.json"))["status"] == "UNAVAILABLE"
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"status": "LOCKED", "generated_at": "2026-11-22T20:00:00+00:00", "scoreboard": {"calls": 0}}))
    now = datetime(2026, 11, 22, 20, 5, tzinfo=timezone.utc)
    assert json.loads(m6.latest_body(f, now=now))["status"] == "STALE"
    assert json.loads(m6.latest_body(f, now=now - timedelta(minutes=4, seconds=50)))["status"] == "LOCKED"


def test_dashboard_minute6_panel_renders_runner_output(tmp_path, monkeypatch):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {}, candles=_candles, log=lambda x: None)
    r.step(_state(), now=OPEN + timedelta(seconds=366), now_mono=0)
    payload = (tmp_path / "m6.json").read_text()
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    start = html.index("Minute-6 panel: reads only /api/minute6")
    block = html[html.rindex("<script>", 0, start) + 8:html.index("</script>", start)]
    assert "innerHTML" not in block
    ids = ["minute6-call", "minute6-status", "minute6-components", "minute6-measured", "minute6-history", "minute6-board"]
    src = ("var els={};" + json.dumps(ids) + ".forEach(function(i){els[i]={textContent:'',className:''};});"
           "var document={getElementById:function(i){return els[i];}};var setInterval=function(){};"
           "var fetch=function(){return Promise.resolve({ok:true,json:function(){return " + payload + ";}});};"
           + block + ";setTimeout(function(){console.log(JSON.stringify(els));},20);")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    els = json.loads(out.stdout)
    assert els["minute6-call"]["textContent"] in ("UP", "DOWN", "WAIT") and "KX-M" in els["minute6-status"]["textContent"]
    assert "MEASURED: BTC" in els["minute6-measured"]["textContent"] and "A price/distance" in els["minute6-components"]["textContent"]
    html_top = html[html.index('<section id="minimal-opening"'):]
    assert html_top.index('id="ai-three-lines"') < html_top.index('id="minute6-card"')


def test_waits_for_the_late_minute6_candle_then_uses_it(tmp_path, monkeypatch):
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    published = {"upto": O + 240}                      # the +5 candle is not out yet
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {},
                      candles=lambda: _candles(upto=published["upto"]), log=lambda x: None)
    assert r.step(_state(), now=OPEN + timedelta(seconds=366), now_mono=0) == "WAITING FOR CANDLE"
    published["upto"] = O + 300
    assert r.step(_state(), now=OPEN + timedelta(seconds=372), now_mono=1) == "SENT"
    assert r.db.execute("SELECT a_dir FROM minute6").fetchone()[0] is not None


def test_missing_candle_at_the_deadline_locks_ineligible_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {},
                      candles=lambda: _candles(upto=O + 240), log=lambda x: None)
    s = Sender()
    r.send = s
    assert r.step(_state(), now=OPEN + timedelta(seconds=412), now_mono=0) == "RECORDED"   # stored, not sent
    assert r.db.execute("SELECT a_dir, direction FROM minute6").fetchone() == (None, "WAIT") and not s.calls


def test_merge_bars_uses_final_15s_bar_and_skips_unfinished_minutes():
    from minute6_runner import merge_bars
    minute = [{"start": 600, "low": 1, "high": 3, "open": 2, "close": 2.5, "volume": 1}]
    b15 = [{"start": 600 + 15 * i, "low": 1, "high": 3, "open": 2, "close": 2.0 + i, "volume": 0.5} for i in range(4)]
    b15 += [{"start": 660 + 15 * i, "low": 4, "high": 6, "open": 5, "close": 5.0 + i, "volume": 0.5} for i in range(4)]
    b15 += [{"start": 720, "low": 7, "high": 7, "open": 7, "close": 7, "volume": 1}]     # unfinished minute
    out = {c[0]: c for c in merge_bars(minute, b15)}
    assert out[600][4] == 5.0                          # close of the bar starting at 600+45
    assert out[660][4] == 8.0 and out[660][3] == 5 and abs(out[660][5] - 2.0) < 1e-12
    assert 720 not in out


def test_latest_json_carries_pending_then_unavailable_lines(tmp_path):
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {}, candles=_candles, log=lambda x: None)
    r.write_latest(_state(), OPEN + timedelta(seconds=120), 0)
    assert json.loads((tmp_path / "m6.json").read_text())["line"] == "MINUTE-6 FINAL: PENDING 🟡"
    r.write_latest(_state(), OPEN + timedelta(seconds=500), 1)
    assert json.loads((tmp_path / "m6.json").read_text())["line"] == "MINUTE-6 FINAL: WAIT 🟡 — N/A"


def test_latest_json_carries_scoreboard7_not_started_then_running(tmp_path):
    import scoreboard7
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {}, candles=_candles,
                      history=lambda: {"markets": []}, log=lambda x: None)
    r.write_latest(_state(), OPEN + timedelta(seconds=120), 0)
    assert json.loads((tmp_path / "m6.json").read_text())["scoreboard7"] == {"status": "NOT STARTED"}
    scoreboard7.start_window(tmp_path / "score_window.json", now=OPEN)
    r.write_latest(_state(), OPEN + timedelta(seconds=130), 1)
    sb7 = json.loads((tmp_path / "m6.json").read_text())["scoreboard7"]
    assert sb7["status"] == "RUNNING" and sb7["models"]["MINUTE-6 FINAL"]["accuracy"] is None


def test_first6_shadow_locks_once_from_pre_open_data_and_records_six_minute_outcome(tmp_path, monkeypatch):
    from src.signals_display import first6 as f6
    monkeypatch.setattr("minute6_runner.time.sleep", lambda s: None)
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {}, candles=_candles, log=lambda x: None)
    assert r.first6_step(_state(), OPEN + timedelta(seconds=5), 0) == "IDLE"          # before the lock window
    assert r.first6_step(_state(), OPEN + timedelta(seconds=12), 1) == "LOCKED"
    assert r.first6_step(_state(), OPEN + timedelta(seconds=20), 2) == "IDLE"         # once only
    row = r.db.execute("SELECT direction, version, reference_open FROM first6").fetchone()
    by = {c[0]: c[4] for c in _candles()}
    assert row[1] == f6.VERSION and row[2] == by[O - 60]                              # minute ending at the open
    assert r.db.execute("SELECT COUNT(*) FROM first6_outcomes").fetchone()[0] == 0
    r.first6_step(_state(), OPEN + timedelta(seconds=370), 3)
    ref0, ref6, out = r.db.execute("SELECT reference_open, reference_6m, outcome FROM first6_outcomes").fetchone()
    assert ref6 == by[O + 300] and out == ("UP" if ref6 > ref0 else "DOWN" if ref6 < ref0 else "FLAT")
    with pytest.raises(sqlite3.DatabaseError):
        r.db.execute("UPDATE first6 SET direction='UP'")


def test_first6_missing_candles_never_invent_a_prediction(tmp_path):
    r = Minute6Runner(str(tmp_path / "a.db"), str(tmp_path / "m6.json"), Sender(), {},
                      candles=lambda: [c for c in _candles() if c[0] != O - 600], log=lambda x: None)
    assert r.first6_step(_state(), OPEN + timedelta(seconds=30), 0) == "IDLE"
    assert r.db.execute("SELECT COUNT(*) FROM first6").fetchone()[0] == 0

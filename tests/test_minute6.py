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
    assert len(s.calls) == 1 and "MINUTE-6 RESEARCH - NOT VALIDATED" in s.calls[0] and "KX-M" in s.calls[0]
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

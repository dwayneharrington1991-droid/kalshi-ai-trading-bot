"""Verified AI performance counter: immutable live UP/DOWN calls vs official settlement."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals_display.monitor import Journal

START = datetime(2026, 11, 1, tzinfo=timezone.utc)
CAL = {"status": "EXPERIMENTAL CALIBRATED CANDIDATE"}


def _opening(journal, i, *, up, direction, confidence=CAL, outcome=None, mode="PUBLIC MARKET DATA",
             model="opening-candidate/abc", payload_override=None):
    opened = START + timedelta(minutes=15 * i)
    ticker = f"KXBTC15M-SB-{i:03d}"
    payload = payload_override if payload_override is not None else json.dumps(
        {"ticker": ticker, "model": model, "up": up, "direction": direction, "confidence": confidence})
    with journal.connect() as db:
        db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                   (mode, ticker, opened.isoformat(), opened.isoformat(),
                    (opened + timedelta(minutes=15)).isoformat(), up, "ON_TIME", payload))
        if outcome is not None:
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (ticker, "official", (opened + timedelta(minutes=16)).isoformat(), outcome))


def test_scoreboard_counts_only_verified_displayed_calls(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    _opening(journal, 0, up=0.72, direction="UP", outcome=1)       # correct
    _opening(journal, 1, up=0.70, direction="UP", outcome=0)       # wrong
    _opening(journal, 2, up=0.25, direction="DOWN", outcome=0)     # correct
    _opening(journal, 3, up=0.61, direction="WAIT", outcome=1)     # WAIT excluded
    _opening(journal, 4, up=0.53, direction="UP", confidence={"status": "UNCALIBRATED"},
             model="opening24h-zero-drift-final-average-v1", outcome=1)  # never displayed as a call
    _opening(journal, 5, up=0.80, direction="UP")                  # unsettled
    _opening(journal, 6, up=0.40, direction="UP", outcome=1)       # side contradicts probability: invalid
    _opening(journal, 7, up=0.9, direction="UP", outcome=1, payload_override="{broken")  # invalid JSON
    _opening(journal, 8, up=0.9, direction="UP", outcome=1, mode="REPLAY / SIMULATED")  # not live
    _opening(journal, 9, up=0.9, direction="UP", confidence={"status": "MADE_UP"}, outcome=1)
    board = journal.verified_opening_scoreboard()
    assert (board["correct"], board["wrong"], board["verified_calls"]) == (2, 1, 3)
    assert abs(board["accuracy"] - 2 / 3) < 1e-12
    assert board["excluded"] == {"wait_or_no_call": 3, "unsettled": 1, "invalid": 2, "duplicate": 0}
    lo, hi = board["accuracy_interval_95"]
    assert lo < 2 / 3 < hi


def test_scoreboard_is_empty_not_invented_without_verified_calls(tmp_path):
    board = Journal(tmp_path / "journal.db").verified_opening_scoreboard()
    assert board["verified_calls"] == 0 and board["accuracy"] is None and board["accuracy_interval_95"] is None


def test_scoreboard_route_cache_invalidation_and_dashboard_wiring():
    root = Path(__file__).resolve().parents[1] / "src" / "signals_display"
    server = (root / "server.py").read_text()
    assert 'elif route == "/api/opening-scoreboard":' in server
    assert "scoreboard.invalidate()" in server
    page = (root / "dashboard.html").read_text()
    for marker in ('id="ai-scoreboard"', 'id="score-correct"', 'id="score-wrong"', 'id="score-accuracy"',
                   'id="score-sample"', "fetch('/api/opening-scoreboard'", "setInterval(refreshScoreboard,30000)",
                   "main>div.ai-scoreboard:not(#minimal-opening){display:flex!important"):
        assert marker in page, marker

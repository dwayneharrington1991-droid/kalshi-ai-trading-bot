"""Dual-AI opening alert: same market only, nothing substituted, one message per ticker, failures recorded."""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("dual", Path(__file__).resolve().parents[1] / "scripts" / "dual_ai_alerts.py")
dual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dual)

OPEN = datetime(2026, 11, 22, 17, 45, tzinfo=timezone.utc)
GEN = OPEN + timedelta(seconds=3)


def _state(ticker="KX-A", up=0.62, gen=GEN):
    return {"prediction_engine": {"initial": {"ticker": ticker, "up": up, "generated_at": gen.isoformat(),
                                              "closes_at": (OPEN + timedelta(minutes=15)).isoformat()}}}


def _ch(ticker="KX-A", live=0.62, up=0.50, status="READY", gen=GEN):
    return {"ticker": ticker, "status": status, "up": up, "live_up": live, "generated_from": gen.isoformat(),
            "closes_at": (OPEN + timedelta(minutes=15)).isoformat()}


class Sender:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def __call__(self, channel, cfg, text):
        self.calls.append(text)
        if len(self.calls) <= self.fail:
            raise RuntimeError("boom")
        return 200, {"ok": True, "result": {"message_id": 100 + len(self.calls)}}


def test_both_calls_same_market_one_message(tmp_path, monkeypatch):
    monkeypatch.setattr(dual.time, "sleep", lambda s: None)
    led, s = dual.Ledger(tmp_path / "s.db"), Sender()
    now = OPEN + timedelta(seconds=10)
    assert dual.step(led, s, {}, _state(), _ch(), now, log=lambda x: None) == "SENT"
    assert dual.step(led, s, {}, _state(), _ch(), now + timedelta(seconds=5), log=lambda x: None) == "DUPLICATE"
    assert len(s.calls) == 1
    t = s.calls[0]
    for part in ("KX-A · locked at open · expires 18:00 UTC", "AI #1: UP 🟢 — 62%",
                 "AI #2: WAIT 🟡 — 50% UP", "COMBINED CALL: WAIT 🟡 — 56% UP", "Manual trading only"):
        assert part in t


def test_mismatched_market_is_never_paired(tmp_path):
    k, _ = dual.opening(_state(), _ch(ticker="KX-OLD"), OPEN + timedelta(seconds=10))
    assert k == "WAITING"
    k, d = dual.opening(_state(), _ch(live=0.61), OPEN + timedelta(seconds=120))
    assert k == "CHALLENGER_UNAVAILABLE"
    assert "AI #2: WAIT 🟡 — N/A" in dual.message(k, d) and "COMBINED CALL: WAIT 🟡 — N/A" in dual.message(k, d)


def test_no_live_call_or_late_or_stale_record_sends_nothing():
    assert dual.opening({}, _ch(), OPEN)[0] == "SKIP"
    assert dual.opening(_state(), _ch(), OPEN + timedelta(minutes=14, seconds=30))[0] == "SKIP"
    old = OPEN - timedelta(minutes=15) + timedelta(seconds=3)       # previous market's record
    assert dual.opening(_state(gen=old), _ch(gen=old), OPEN + timedelta(seconds=10))[0] == "SKIP"


def test_retry_then_failure_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(dual.time, "sleep", lambda s: None)
    led = dual.Ledger(tmp_path / "s.db")
    assert dual.step(led, Sender(fail=1), {}, _state(), _ch(), OPEN + timedelta(seconds=10), log=lambda x: None) == "SENT"
    led2 = dual.Ledger(tmp_path / "t.db")
    assert dual.step(led2, Sender(fail=9), {}, _state(), _ch(), OPEN + timedelta(seconds=10), log=lambda x: None) == "FAILED"
    assert led2.db.execute("select status, error from deliveries").fetchone()[0] == "FAILED"


def test_consensus_rule():
    assert dual.combine(0.60, 0.58)[1] == "UP" and dual.combine(0.40, 0.42)[1] == "DOWN"
    assert dual.combine(0.70, 0.54)[1] == "WAIT"          # average 0.62 but challenger did not call UP
    assert dual.combine(0.30, 0.46)[1] == "WAIT"
    assert dual.combine(0.62, 0.40)[1] == "WAIT"
    assert abs(dual.combine(0.62, 0.50)[0] - 0.56) < 1e-12


def test_down_consensus_message_shows_down_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr(dual.time, "sleep", lambda s: None)
    s = Sender()
    assert dual.step(dual.Ledger(tmp_path / "s.db"), s, {}, _state(up=0.40), _ch(live=0.40, up=0.30),
                     OPEN + timedelta(seconds=10), log=lambda x: None) == "SENT"
    assert "COMBINED CALL: DOWN 🔴 — 65%" in s.calls[0] and "AI #2: DOWN 🔴 — 70%" in s.calls[0]


def test_stored_calls_are_immutable_and_legacy_markets_not_resent(tmp_path, monkeypatch):
    import sqlite3
    import pytest
    monkeypatch.setattr(dual.time, "sleep", lambda s: None)
    path = tmp_path / "s.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE sent(ticker TEXT PRIMARY KEY)")
    old.execute("INSERT INTO sent VALUES ('KX-OLD')")
    old.commit()
    led, s = dual.Ledger(path), Sender()
    now = OPEN + timedelta(seconds=10)
    assert dual.step(led, s, {}, _state(ticker="KX-OLD"), _ch(ticker="KX-OLD"), now, log=lambda x: None) == "DUPLICATE"
    assert dual.step(led, s, {}, _state(), _ch(), now, log=lambda x: None) == "SENT" and len(s.calls) == 1
    with pytest.raises(sqlite3.DatabaseError):
        led.db.execute("UPDATE openings SET combined='UP'")
    with pytest.raises(sqlite3.DatabaseError):
        led.db.execute("DELETE FROM deliveries")


def test_builtin_notifier_steps_aside_only_when_the_service_marker_exists(tmp_path, monkeypatch):
    from src.signals_display import phone_alerts
    monkeypatch.delenv("ALERTS_DISABLED", raising=False)
    marker = tmp_path / "OWNS_OPENING_ALERTS"
    monkeypatch.setattr(phone_alerts, "OWNER_MARKER", marker)
    monkeypatch.setattr(phone_alerts.OpeningAlerts, "__init__", lambda self, *a, **k: None)
    assert phone_alerts.from_journal(tmp_path / "j.db") is not None
    marker.write_text("three-line service")
    assert phone_alerts.from_journal(tmp_path / "j.db") is None


def test_market_history_follows_the_api_cursor():
    pages = {"/api/market-history": {"markets": [{"ticker": "A"}], "pagination": {"has_more": True,
             "next_before_close": "2026-11-22T18:45:00+00:00", "next_before_ticker": "A"}}}
    seen = []

    def fetch(path):
        seen.append(path)
        return pages.get(path, {"markets": [{"ticker": "B"}], "pagination": {"has_more": False}})
    out = dual.market_history(pages=3, fetch=fetch)
    assert [m["ticker"] for m in out["markets"]] == ["A", "B"] and "before_close=2026-11-22T18%3A45%3A00%2B00%3A00" in seen[1]

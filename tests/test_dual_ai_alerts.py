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
    for part in ("KX-A", "Opened 17:45 UTC", "Expires 18:00 UTC", "LIVE AI: UP - P(UP) 62.0%",
                 "CHALLENGER AI: WAIT - P(UP) 50.0%", "DISAGREE", "NOT VALIDATED"):
        assert part in t


def test_mismatched_market_is_never_paired(tmp_path):
    k, _ = dual.opening(_state(), _ch(ticker="KX-OLD"), OPEN + timedelta(seconds=10))
    assert k == "WAITING"
    k, d = dual.opening(_state(), _ch(live=0.61), OPEN + timedelta(seconds=120))
    assert k == "CHALLENGER_UNAVAILABLE"
    assert "UNAVAILABLE" in dual.message(k, d) and "no call substituted" in dual.message(k, d)


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
    assert led2.db.execute("select status, error from sent").fetchone()[0] == "FAILED"

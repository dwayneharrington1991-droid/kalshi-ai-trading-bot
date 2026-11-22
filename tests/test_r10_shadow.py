"""R10 shadow: corrected inputs computed in parallel, write-once, never on the live path."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.signals_display.r10_shadow import R10Shadow, ordered_batches

T0 = datetime(2026, 11, 22, 4, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _quote(i, at, rec):
    return {"source": "Coinbase BTC/USD stream", "kind": "quote", "price": 60000 + i, "bid": 59999 + i,
            "ask": 60001 + i, "bid_depth": 1.0, "ask_depth": 1.2, "event_at": at.isoformat(),
            "received_at": rec.isoformat(), "session_id": "s"}


def _trade(tid, at, rec, side="buy"):
    return {"source": "Coinbase BTC/USD stream", "kind": "trade", "price": 60000.5, "volume": 0.01,
            "aggressor": side, "trade_id": str(tid), "event_at": at.isoformat(), "received_at": rec.isoformat(),
            "session_id": "s"}


def _stream(minutes=20):
    """Quotes every second and a newest-first 3-trade batch every 2 s, as Coinbase delivers them."""
    obs, tid = [], 0
    for s in range(minutes * 60):
        at = T0 - timedelta(minutes=minutes) + timedelta(seconds=s)
        obs.append(_quote(s % 7, at, at + timedelta(milliseconds=50)))
        if s % 2 == 0:
            rec = at + timedelta(milliseconds=900)
            batch = [_trade(tid + k, at + timedelta(milliseconds=100 + 200 * k), rec, "buy" if k % 2 else "sell")
                     for k in range(3)]
            tid += 3
            obs.extend(reversed(batch))                       # newest first
    return obs


def test_ordered_batches_sorts_only_within_one_message_and_keeps_receipt_order():
    obs = _stream(1)
    out = ordered_batches(obs)
    assert sorted(map(json.dumps, out)) == sorted(map(json.dumps, obs))        # same observations
    receipts = [o["received_at"] for o in out]
    assert receipts == sorted(receipts)
    trades = [o for o in out if o["kind"] == "trade"]
    assert [o["event_at"] for o in trades] == sorted(o["event_at"] for o in trades)


def test_shadow_keeps_valid_trades_the_live_consumers_reject(tmp_path):
    from src.signals_display.intelligence import MarketIntelligence
    live = MarketIntelligence()
    live_rejected = 0
    for o in _stream(5):
        try:
            live.observe(o)
        except ValueError:
            live_rejected += 1
    shadow = R10Shadow(tmp_path / "r10.sqlite3")
    shadow.observe_batch(_stream(5))
    assert live_rejected > 0
    assert shadow.counters["intelligence_rejected"] == 0 and shadow.counters["advanced_rejected"] == 0


def test_duplicates_and_stale_trades_are_still_rejected(tmp_path):
    shadow = R10Shadow(tmp_path / "r10.sqlite3")
    q = _quote(0, T0, T0 + timedelta(milliseconds=50))
    t = _trade(1, T0 + timedelta(milliseconds=10), T0 + timedelta(milliseconds=900))
    stale = _trade(2, T0 - timedelta(seconds=30), T0 + timedelta(seconds=1))
    shadow.observe_batch([q, t, t, stale])
    assert shadow.intelligence.sources["Coinbase BTC/USD stream"].rejected >= 0
    assert shadow.counters["intelligence_rejected"] >= 1                     # stale (> 5 s at receipt)


def test_record_is_write_once_and_uses_only_inputs_before_the_opening(tmp_path):
    path = tmp_path / "r10.sqlite3"
    shadow = R10Shadow(path)
    shadow.observe_batch(_stream(20))
    shadow.record("KX-TEST", T0, primary_source="Coinbase BTC/USD stream")
    shadow.observe_batch([_trade(10**6, T0 + timedelta(seconds=5), T0 + timedelta(seconds=6))])
    shadow.record("KX-TEST", T0 + timedelta(minutes=1), primary_source="Coinbase BTC/USD stream")
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT ticker, opens_at, values_json FROM r10_shadow_openings").fetchall()
        assert len(rows) == 1 and rows[0][1] == T0.isoformat()
        values = json.loads(rows[0][2])
        assert any(k.startswith("advanced_") for k in values) and any(k.startswith("micro_") for k in values)
        try:
            db.execute("DELETE FROM r10_shadow_openings")
            raise AssertionError("delete must be refused")
        except sqlite3.DatabaseError:
            pass
    assert shadow.counters["record_errors"] == 0


def test_record_errors_are_contained(tmp_path):
    shadow = R10Shadow(tmp_path / "missing-dir" / "r10.sqlite3")
    shadow.record("KX", T0)                                                   # unwritable path
    assert shadow.counters["record_errors"] == 1


def test_server_feeds_the_shadow_after_live_consumers_and_records_after_the_live_opening():
    src = (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")
    feed = src.index("self.r10_shadow.observe_batch(")
    assert src.index("for consumer in (self.intelligence.observe, self.advanced.observe):") < feed
    rec = src.index("self.r10_shadow.record_async(")
    assert src.index("opening = self.journal.opening_record(\n", 0) < rec or "opening_record(" in src[rec - 400:rec]
    assert "MarketIntelligence(per_kind_event_clock" not in src and "sort_trade_batches" not in src

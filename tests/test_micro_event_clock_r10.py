"""R10: a trade whose exchange time trails the latest quote. Production (default) behaviour is pinned;
the per-kind clock is a candidate switch that is off unless explicitly enabled."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display.intelligence import MarketIntelligence

T0 = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)


def _feed(mi):
    mi.ingest("coinbase", 60000, event_at=T0 + timedelta(seconds=1.0), received_at=T0 + timedelta(seconds=1.1),
              bid=59999, ask=60001, session_id="s")
    # Trade executed at +0.8 s, delivered after the +1.0 s quote (normal for a separate trade channel).
    return dict(price=60000.5, event_at=T0 + timedelta(seconds=0.8), received_at=T0 + timedelta(seconds=1.2),
                kind="trade", trade_id="t1", volume=0.01, aggressor="buy", session_id="s")


def test_default_keeps_production_behaviour_and_rejects_the_delayed_trade():
    mi = MarketIntelligence()
    with pytest.raises(ValueError, match="Out-of-order exchange timestamp"):
        mi.ingest("coinbase", **_feed(mi))
    assert mi.sources["coinbase"].rejected == 1


def test_per_kind_clock_accepts_the_delayed_trade_but_still_orders_each_kind():
    mi = MarketIntelligence(per_kind_event_clock=True)
    assert mi.ingest("coinbase", **_feed(mi)) is True
    assert mi.sources["coinbase"].rejected == 0
    with pytest.raises(ValueError, match="Out-of-order exchange timestamp"):     # older trade than the last trade
        mi.ingest("coinbase", 60000, event_at=T0 + timedelta(seconds=0.5), received_at=T0 + timedelta(seconds=1.3),
                  kind="trade", trade_id="t0", volume=0.01, aggressor="sell", session_id="s")
    with pytest.raises(ValueError, match="Out-of-order exchange timestamp"):     # older quote than the last quote
        mi.ingest("coinbase", 60000, event_at=T0 + timedelta(seconds=0.9), received_at=T0 + timedelta(seconds=1.4),
                  bid=59999, ask=60001, session_id="s")


def test_reconnect_clears_both_clocks():
    mi = MarketIntelligence(per_kind_event_clock=True)
    mi.ingest("coinbase", **_feed(mi))
    mi.reset("coinbase", session_id="s2")
    assert mi.sources["coinbase"].last_event_by_kind == {}


def test_production_server_does_not_enable_the_candidate_switch():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/signals_display/server.py").read_text(encoding="utf-8")
    assert "self.intelligence = MarketIntelligence()" in src and "per_kind_event_clock" not in src


def _batch():
    from tests.test_signals_display import NOW
    rows = [(3, 0.30), (2, 0.20), (1, 0.10)]                     # newest first, as Coinbase sends them
    trades = [{"product_id": "BTC-USD", "trade_id": str(i), "price": "60000", "size": "0.01", "side": "BUY",
               "time": (NOW + timedelta(seconds=s)).isoformat().replace("+00:00", "Z")} for i, s in rows]
    return NOW, {"channel": "market_trades", "sequence_num": 0, "events": [{"type": "update", "trades": trades}]}


def _published(hub):
    out = []
    hub._publish_coinbase = lambda obs: out.append(obs["trade_id"])
    now, msg = _batch()
    hub.process_coinbase(msg, now + timedelta(seconds=1), 1.0)
    return out


def test_stream_default_keeps_the_exchange_batch_order():
    from src.signals_display.streaming import StreamHub
    assert _published(StreamHub(autostart=False)) == ["3", "2", "1"]


def test_stream_candidate_publishes_batches_oldest_first():
    from src.signals_display.streaming import StreamHub
    assert _published(StreamHub(autostart=False, sort_trade_batches=True)) == ["1", "2", "3"]


def test_full_candidate_keeps_every_valid_trade_of_a_newest_first_batch():
    from src.signals_display.streaming import StreamHub
    now, msg = _batch()
    for sort, per_kind, kept in ((False, False, 1), (True, True, 3)):
        mi = MarketIntelligence(per_kind_event_clock=per_kind)
        hub = StreamHub(autostart=False, sort_trade_batches=sort)
        accepted = []

        def publish(obs, mi=mi, accepted=accepted):
            try:
                accepted.append(mi.observe({k: v for k, v in obs.items() if k in (
                    "source", "price", "event_at", "received_at", "kind", "volume", "aggressor", "trade_id", "session_id")}))
            except ValueError:
                pass
        hub._publish_coinbase = publish
        hub.process_coinbase(msg, now + timedelta(seconds=1), 1.0)
        assert sum(accepted) == kept


def test_production_monitor_does_not_enable_batch_sorting():
    src = (Path(__file__).resolve().parents[1] / "src/signals_display/monitor.py").read_text(encoding="utf-8")
    assert "sort_trade_batches" not in src

"""R10: a trade whose exchange time trails the latest quote. Production (default) behaviour is pinned;
the per-kind clock is a candidate switch that is off unless explicitly enabled."""
from datetime import datetime, timedelta, timezone

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

"""A real Kraken dust trade must not reset the spot session.

Found 2026-11-20: Kraken prints trades smaller than 0.000001 BTC. Flow.trade
rejected them ("numeric value outside supported bounds"), which raised out of
the socket loop, reset CVD and the session, and reconnected. Production recorded
2,273 Kraken spot session resets in about 15.5 h (about one every 25 s) against
6 for the futures feed, so the spot micro/flow windows kept restarting.
"""
from datetime import timedelta

import pytest

from src.signals_display.order_flow import Flow
from tests.test_signals_display import NOW


def test_dust_trade_is_recorded_without_resetting_the_session():
    flow = Flow("BTC")
    flow.trade("1", NOW, 90000, 0.01, "buy", NOW)
    started = flow.started
    flow.trade("2", NOW + timedelta(milliseconds=10), 90000.1, 0.00000005, "sell", NOW + timedelta(milliseconds=20))
    assert flow.started == started and len(flow.trades) == 2
    assert flow.cumulative == pytest.approx(0.01 - 0.00000005)


@pytest.mark.parametrize("qty", [0, -0.1, float("nan"), float("inf")])
def test_invalid_quantities_are_still_rejected(qty):
    with pytest.raises(ValueError):
        Flow("BTC").trade("1", NOW, 90000, qty, "buy", NOW)

"""Coinbase Advanced Trade numbers messages once per CONNECTION, across all channels.

Verified 2026-11-20 on the public feed: sequence_num rose by exactly 1 across
subscriptions, ticker, market_trades and heartbeats messages on one socket.
The adapter checked it per channel, so almost every message looked like a gap
(162 false gaps in 15 s). Production recorded 33,778 reconnects in about 15.5 h,
77,007 "missing" updates and 803,395 discarded snapshot trades. Each reconnect
started a new session, which reset the microstructure and flow windows and left
the micro model inputs missing at most openings.
"""
from datetime import timedelta

import pytest

from src.signals_display.streaming import StreamHub
from tests.test_signals_display import NOW
from tests.test_signals_streaming import ticker, trade

# Channel order observed on the live feed (sequence = position).
LIVE_ORDER = ["subscriptions", "ticker", "subscriptions", "market_trades", "subscriptions",
              "market_trades", "ticker", "market_trades", "ticker", "heartbeats", "market_trades",
              "ticker", "market_trades", "ticker", "ticker", "market_trades", "market_trades"]


def _message(channel, seq, i):
    at = NOW + timedelta(milliseconds=50 * i)
    if channel == "ticker":
        return ticker(seq, at, price=str(100000 + i))
    if channel == "market_trades":
        return trade(seq, at, identifier=f"t{i}")
    return {"channel": channel, "sequence_num": seq, "events": []}


def test_interleaved_channels_with_connection_wide_sequence_never_reconnect():
    hub = StreamHub(autostart=False)
    session = hub.sessions["coinbase"]
    for i, channel in enumerate(LIVE_ORDER):
        hub.process_coinbase(_message(channel, i, i), NOW + timedelta(milliseconds=50 * i + 5), 1 + 0.05 * i)
    assert hub.metrics["missing_updates"] == 0
    assert hub.metrics["rejected_messages"] == 0
    assert hub.sessions["coinbase"] == session
    assert hub.feeds["Coinbase BTC/USD stream"]["connected"] is True


def test_a_real_connection_wide_gap_still_requires_reconnect():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(_message("ticker", 0, 0), NOW, 1)
    hub.process_coinbase(_message("market_trades", 1, 1), NOW + timedelta(milliseconds=55), 1.05)
    with pytest.raises(ValueError):
        hub.process_coinbase(_message("ticker", 3, 3), NOW + timedelta(milliseconds=155), 1.15)   # 2 missing
    assert hub.metrics["missing_updates"] == 1


def test_duplicate_and_out_of_order_sequences():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(_message("ticker", 0, 0), NOW, 1)
    assert hub.process_coinbase(_message("ticker", 0, 0), NOW, 1) is False                       # duplicate
    hub.process_coinbase(_message("market_trades", 1, 1), NOW + timedelta(milliseconds=55), 1.05)
    with pytest.raises(ValueError):
        hub.process_coinbase(_message("ticker", 0, 2), NOW + timedelta(milliseconds=105), 1.1)     # went backwards


def test_a_new_connection_restarts_the_sequence():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(_message("ticker", 0, 0), NOW, 1)
    hub.process_coinbase(_message("market_trades", 1, 1), NOW + timedelta(milliseconds=55), 1.05)
    hub.begin_session("coinbase")
    assert hub.process_coinbase(_message("ticker", 0, 2), NOW + timedelta(milliseconds=105), 1.1)

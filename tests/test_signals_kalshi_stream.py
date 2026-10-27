"""Read-only Kalshi market stream fixtures; no broker account or real keys."""
from copy import deepcopy
from datetime import timedelta
import json
from time import monotonic

import pytest

from src.signals_display.kalshi_market_stream import KalshiMarketStream, URL
from tests.test_signals_display import NOW

TICKER = "KXBTC15M-MOCK"


class Client:
    def permissions(self):
        return {"read_only_verified": True}

    def websocket_headers(self):
        return {"KALSHI-ACCESS-KEY": "MOCK_SECRET_NOT_FOR_SNAPSHOT"}


def stream():
    s = KalshiMarketStream(Client(), autostart=False)
    s.set_tickers([TICKER])
    return s


def snapshot(seq=1, ticker=TICKER, sid=1):
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.4000", "3.25"]],
            # Explicit use_yes_price=True means this is a YES ask at $0.70.
            "no_dollars_fp": [["0.7000", "6.50"]],
            "timestamp": NOW.isoformat(),
        },
    }


def delta(seq=2, **values):
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "side": "no",
            "price_dollars": "0.7000",
            "delta_fp": "1.50",
            **values,
        },
    }


def test_only_market_book_subscription_and_explicit_yes_leg_pricing():
    s = stream()
    cmd = s.subscription()
    assert URL == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    assert cmd == {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": [TICKER],
            "use_yes_price": True,
        },
    }
    s.process(snapshot(), now=NOW)
    s.process(delta(), now=NOW + timedelta(milliseconds=250))
    book = s.snapshot(TICKER, NOW + timedelta(milliseconds=300))
    assert book["status"] == "LIVE"
    assert book["quotes"]["up"] == {"bid": 0.4, "ask": 0.7, "bid_size": 3.25, "ask_size": 8.0}
    assert book["quotes"]["down"] == {"bid": 0.3, "ask": 0.6, "bid_size": 8.0, "ask_size": 3.25}
    assert "SECRET" not in json.dumps(book)
    # The new frame did not provide an exchange timestamp: receipt is not an event.
    assert book["event_time"] is None and book["feed_delay_ms"] is None


def test_legacy_units_are_explicit_and_contradictory_formats_fail_closed():
    s = stream()
    legacy = snapshot()
    legacy["msg"] = {"market_ticker": TICKER, "yes": [[40, 3]], "no": [[70, 6]]}
    s.process(legacy, now=NOW)
    assert s.snapshot(TICKER, NOW)["quotes"]["up"]["ask"] == 0.7
    update = delta()
    update["msg"] = {"market_ticker": TICKER, "side": "no", "price": 70, "delta": -1}
    s.process(update, now=NOW)
    assert s.snapshot(TICKER, NOW)["quotes"]["up"]["ask_size"] == 5
    bad = snapshot(seq=3)
    bad["msg"]["yes"] = [[41, 3.25]]
    with pytest.raises(ValueError, match="contradictory"):
        s.process(bad, now=NOW)
    assert s.snapshot(TICKER, NOW)["quotes"] is None


def test_sequence_gap_and_contradictory_duplicate_require_fresh_snapshot():
    s = stream()
    first = snapshot()
    assert s.process(first, now=NOW)
    received = s.snapshot(TICKER, NOW)["observed_at"]
    assert not s.process(first, now=NOW + timedelta(seconds=1))
    assert s.snapshot(TICKER, NOW)["observed_at"] == received
    assert s.duplicates == 1
    with pytest.raises(ValueError, match="sequence gap"):
        s.process(delta(seq=3), now=NOW)
    assert s.sequence_gaps == 1 and s.snapshot(TICKER, NOW)["quotes"] is None
    with pytest.raises(ValueError, match="before matching snapshot"):
        s.process(delta(seq=4), now=NOW)
    s.process(snapshot(), now=NOW)
    altered = snapshot()
    altered["msg"]["yes_dollars_fp"][0][1] = "999"
    with pytest.raises(ValueError, match="Contradictory duplicate"):
        s.process(altered, now=NOW)


def test_sequences_are_per_subscription_across_multiple_held_markets():
    s = stream()
    other = "KXBTC15M-OLDER"
    s.set_tickers([TICKER, other])
    s.process(snapshot(ticker=TICKER), now=NOW)
    s.process(snapshot(seq=2, ticker=other), now=NOW)
    s.process(delta(seq=3), now=NOW)
    assert s.snapshot(other, NOW)["status"] == "LIVE"
    assert s.snapshot(TICKER, NOW)["quotes"]["up"]["ask_size"] == 8
    # Main rollover plus a still-held old contract always requests new snapshots.
    s.set_tickers([other, "KXBTC15M-NEXT"])
    assert s.snapshot(other, NOW)["quotes"] is None
    assert s.subscription()["params"]["market_tickers"] == ["KXBTC15M-NEXT", other]


@pytest.mark.parametrize(
    "values",
    [
        {"side": "unknown"},
        {"delta_fp": "-99"},
        {"price_dollars": "NaN"},
        {"price_dollars": "0.70", "price": 60},
        {"delta_fp": "1", "delta": 2},
    ],
)
def test_invalid_delta_never_leaves_a_live_book(values):
    s = stream()
    s.process(snapshot(), now=NOW)
    with pytest.raises(ValueError):
        s.process(delta(**values), now=NOW)
    assert s.snapshot(TICKER, NOW)["quotes"] is None


def test_timestamps_staleness_and_crossed_or_incomplete_depth_are_honest():
    s = stream()
    s.process(snapshot(), now=NOW + timedelta(milliseconds=25))
    assert s.snapshot(TICKER, NOW + timedelta(milliseconds=25))["feed_delay_ms"] == 25
    assert s.snapshot(TICKER, NOW + timedelta(seconds=6))["status"] == "STALE"
    # A subscription acknowledgement cannot refresh the old quote.
    s.process({"type": "subscribed", "msg": {"sid": 1}}, now=NOW + timedelta(seconds=6))
    assert s.snapshot(TICKER, NOW + timedelta(seconds=6))["quotes"] is None
    future = snapshot(seq=2)
    future["msg"]["timestamp"] = (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        s.process(future, now=NOW)
    crossed = snapshot()
    crossed["msg"]["yes_dollars_fp"] = [["0.80", "1"]]
    with pytest.raises(ValueError, match="Crossed"):
        s.process(crossed, now=NOW)
    empty = snapshot()
    empty["msg"]["no_dollars_fp"] = []
    s.process(empty, now=NOW)
    assert s.snapshot(TICKER, NOW)["quotes"] is None


def test_subscription_scope_is_bounded_and_no_other_market_or_commands_exist():
    s = stream()
    for bad in (
        ["KXETH15M-MOCK"],
        ["KXBTC15M-../../orders"],
        ["KXBTC15M-A"] * 2 + [False],
        [f"KXBTC15M-{i}" for i in range(25)],
    ):
        with pytest.raises(ValueError):
            s.set_tickers(bad)
    s.set_tickers([TICKER] * 50)  # Actual unique subscriptions, not duplicate count.
    assert s.subscription()["params"]["market_tickers"] == [TICKER]
    with pytest.raises(ValueError, match="Unrequested"):
        s.process(snapshot(ticker="KXBTC15M-NOT-REQUESTED"), now=NOW)
    for attribute in ("place_order", "cancel_order", "transfer", "send", "request", "amend_order"):
        assert not hasattr(s, attribute)


def test_worker_verifies_permissions_before_upgrade_and_redacts_failures():
    s = stream()

    class Denied:
        def permissions(self):
            s.stop.set()
            return {"read_only_verified": False, "blocker": "PRIVATE SECRET"}

        def websocket_headers(self):
            pytest.fail("Credentials must not be read before permission verification")

    def forbidden(*args, **kwargs):
        pytest.fail("Must not connect with unverified permissions")

    s.client, s.connector = Denied(), forbidden
    s.run()
    assert "SECRET" not in json.dumps(s.snapshot(TICKER, NOW))
    assert s.snapshot(TICKER, NOW)["quotes"] is None


def test_mock_worker_sends_only_allowlisted_subscribe_and_closes_cleanly():
    s = stream()
    sent, connections = [], []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send(self, message):
            sent.append(json.loads(message))

        def recv(self, **kwargs):
            assert kwargs["timeout"] == 0.5
            s.stop.set()
            return json.dumps(snapshot())

    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return Socket()

    s.connector = connect
    s.run()
    assert len(sent) == 1 and sent[0] == s.subscription()
    assert connections[0][0] == URL
    assert connections[0][1]["max_queue"] == 16
    assert connections[0][1]["additional_headers"] == {
        "KALSHI-ACCESS-KEY": "MOCK_SECRET_NOT_FOR_SNAPSHOT"
    }
    assert "MOCK_SECRET" not in json.dumps(s.snapshot(TICKER, NOW))
    sequence = s.sequence
    assert s.wait_for_update(sequence, timeout=0) == sequence
    s.close()
    assert s.snapshot(TICKER, NOW)["quotes"] is None

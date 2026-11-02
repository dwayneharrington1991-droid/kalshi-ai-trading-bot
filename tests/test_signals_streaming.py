"""Public stream schema, bounded queues and recovery; no network or credentials."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import json
import threading

import pytest

from src.signals_display.streaming import StreamHub, COINBASE_WS
from src.signals_display.order_flow import Flow
from tests.test_signals_display import NOW


def ticker(seq=0, at=NOW, price="100001"):
    return {
        "channel": "ticker",
        "sequence_num": seq,
        "timestamp": at.isoformat(),
        "events": [
            {
                "type": "update",
                "tickers": [
                    {
                        "product_id": "BTC-USD",
                        "price": price,
                        "best_bid": "100000",
                        "best_ask": "100002",
                        "best_bid_quantity": "2",
                        "best_ask_quantity": "3",
                    }
                ],
            }
        ],
    }


def trade(seq=0, at=NOW, identifier="t1", side="BUY"):
    return {
        "channel": "market_trades",
        "sequence_num": seq,
        "timestamp": at.isoformat(),
        "events": [
            {
                "type": "update",
                "trades": [
                    {
                        "product_id": "BTC-USD",
                        "trade_id": identifier,
                        "price": "100001",
                        "size": "0.25",
                        "side": side,
                        "time": at.isoformat(),
                    }
                ],
            }
        ],
    }


def test_coinbase_public_ticker_receipt_event_time_and_quote_depth():
    hub = StreamHub(autostart=False)
    assert hub.snapshot(NOW)["reference"] is None
    event = NOW - timedelta(milliseconds=120)
    assert hub.process_coinbase(ticker(at=event), NOW, 1)
    state = hub.snapshot(NOW)
    assert state["reference"]["price"] == 100001
    assert state["reference"]["observed_at"] == NOW.isoformat()
    assert state["reference"]["event_time"] == event.isoformat()
    feed = state["feeds"]["Coinbase BTC/USD stream"]
    assert feed["event_to_observed_ms"] == pytest.approx(120)
    assert feed["data"]["bid_size"] == 2 and feed["data"]["ask_size"] == 3
    assert state["diagnostics"]["last_processing_ms"] is not None
    assert not state["reference"]["official"]


def test_coinbase_market_trades_side_is_maker_not_aggressor():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(trade(side="BUY"), NOW, 1)
    hub.process_coinbase(trade(1, NOW, "t2", "SELL"), NOW, 1)
    observations = hub.drain_observations()
    assert [item["aggressor"] for item in observations] == ["sell", "buy"]
    assert [item["volume"] for item in observations] == [0.25, 0.25]
    assert all(item["receipt_basis"] == "actual local socket receipt" for item in observations)


def test_duplicates_do_not_invent_volume_or_price_updates():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(trade(), NOW, 1)
    sequence = hub.sequence
    assert not hub.process_coinbase(trade(), NOW, 1)
    assert hub.sequence == sequence
    assert not hub.process_coinbase(trade(1), NOW, 1)
    assert len(hub.drain_observations()) == 1


def test_heartbeat_and_trade_do_not_refresh_old_quote_timestamps():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(ticker(), NOW, 1)
    after = NOW + timedelta(seconds=6)
    heartbeat = {"channel": "heartbeats", "sequence_num": 0, "events": []}
    hub.process_coinbase(heartbeat, after, 7)
    assert hub.snapshot(after)["reference"] is None
    hub.process_coinbase(trade(at=after), after, 7)
    feed = hub.snapshot(after)["feeds"]["Coinbase BTC/USD stream"]
    assert feed["status"] == "LIVE" and feed["data"]["bid"] is None and feed["data"]["ask"] is None


@pytest.mark.parametrize("change", ["gap", "future", "unknown_side", "crossed", "nonfinite"])
def test_bad_or_incomplete_source_data_is_rejected_and_reference_disabled(change):
    hub = StreamHub(autostart=False)
    hub.process_coinbase(ticker(), NOW, 1)
    if change == "gap":
        packet = ticker(2)
    elif change == "future":
        packet = ticker(1, NOW + timedelta(milliseconds=1))
    elif change == "unknown_side":
        packet = trade(side="UNKNOWN")
    elif change == "crossed":
        packet = ticker(1)
        packet["events"][0]["tickers"][0]["best_bid"] = "100100"
    else:
        packet = ticker(1, price="NaN")
    with pytest.raises(ValueError):
        hub.process_coinbase(packet, NOW, 1)
    assert hub.snapshot(NOW)["reference"] is None
    assert hub.metrics["rejected_messages"] == 1
    if change == "gap":
        assert hub.metrics["missing_updates"] == 1


def test_bounded_backpressure_has_explicit_gap_marker_and_latest_price():
    hub = StreamHub(autostart=False, max_pending=2)
    for i in range(5):
        hub.process_coinbase(trade(i, NOW, str(i)), NOW, 1)
    assert len(hub.pending) == 2 and hub.metrics["queue_dropped"] == 3
    items = hub.drain_observations()
    assert items[0]["gap_detected"] and items[0]["missing_updates_count"] == 3
    assert [item["trade_id"] for item in items] == ["3", "4"]
    assert hub.snapshot(NOW)["reference"]["price"] == 100001
    assert hub.drain_observations() == []


def test_reconnect_requires_fresh_data_and_cannot_reuse_previous_quote():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(ticker(), NOW, 1)
    previous_session = hub.snapshot(NOW)["reference"]["session_id"]
    hub.disconnected(TimeoutError())
    assert hub.snapshot(NOW)["reference"] is None
    hub.begin_session("coinbase")
    assert hub.snapshot(NOW)["reference"] is None
    hub.process_coinbase(trade(), NOW, 1)
    after = hub.snapshot(NOW)
    assert after["reference"]["session_id"] != previous_session
    assert after["feeds"]["Coinbase BTC/USD stream"]["data"]["bid"] is None


def test_delayed_cross_channel_trade_does_not_rewind_latest_reference():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(ticker(at=NOW, price="100005"), NOW, 1)
    hub.process_coinbase(trade(at=NOW - timedelta(milliseconds=100)), NOW, 1)
    assert hub.snapshot(NOW)["reference"]["price"] == 100005
    assert len(hub.drain_observations()) == 2  # Real trade still retained for flow.


def test_clock_discontinuity_is_detected_using_monotonic_time():
    hub = StreamHub(autostart=False)
    hub.process_coinbase(ticker(), NOW, 1)
    with pytest.raises(ValueError):
        hub.process_coinbase(ticker(1, NOW + timedelta(seconds=5)), NOW + timedelta(seconds=5), 1.1)
    assert "CLOCK DISCONTINUITY" in hub.metrics["clock_status"]
    assert hub.snapshot(NOW + timedelta(seconds=5))["reference"] is None
    hub.begin_session("coinbase")
    hub.process_coinbase(ticker(0, NOW + timedelta(seconds=5)), NOW + timedelta(seconds=5), 2)
    assert hub.snapshot(NOW + timedelta(seconds=5))["reference"] is not None


def test_initial_historical_trade_snapshot_does_not_fake_freshness_or_loop_reconnects():
    hub = StreamHub(autostart=False)
    historical = trade(at=NOW - timedelta(minutes=1))
    historical["events"][0]["type"] = "snapshot"
    assert not hub.process_coinbase(historical, NOW, 1)
    assert hub.snapshot(NOW)["reference"] is None
    assert hub.metrics["historical_snapshot_records_excluded"] == 1
    assert hub.process_coinbase(trade(1, NOW, "fresh"), NOW, 1)
    assert hub.snapshot(NOW)["reference"] is not None


def test_existing_kraken_sockets_are_reused_and_sample_time_is_not_network_receipt():
    spot, perp = Flow("BTC"), Flow("raw")
    spot.trade("s1", NOW, 100000, 0.5, "buy", NOW)
    perp.trade("p1", NOW, 100010, 2, "sell", NOW)
    existing = SimpleNamespace(
        lock=threading.Lock(),
        spot=spot,
        perp=perp,
        book=SimpleNamespace(
            ready=True,
            at=NOW,
            bids={Decimal(99999 - i): Decimal(2) for i in range(25)},
            asks={Decimal(100001 + i): Decimal(1) for i in range(25)},
        ),
    )
    hub = StreamHub(autostart=False, order_flow=existing)
    snap = hub.snapshot(NOW)
    assert snap["reference"]["price"] == 100000
    records = hub.drain_observations()
    assert len(records) == 3 and records[-1]["depth_levels"] == 20
    assert all(
        "original socket receipt timestamp unavailable" in item["receipt_basis"] for item in records
    )
    assert hub.snapshot(NOW)["feeds"]["Kraken futures stream"]["status"] == "LIVE"
    assert hub.drain_observations() == []
    spot.reset()
    assert hub.snapshot(NOW)["reference"] is None


def test_public_connection_sends_only_fixed_anonymous_subscriptions_and_can_close():
    sent, connect_args = [], []
    hub = StreamHub(autostart=False)

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, value):
            sent.append(json.loads(value))

        def recv(self, timeout):
            hub.stop.set()
            return json.dumps(ticker())

    def connect(url, **kwargs):
        connect_args.append((url, kwargs))
        return Socket()

    hub.connector = connect
    # Pass NOW as explicit parser receipt without making an external request.
    original = hub.process_coinbase
    hub.process_coinbase = lambda raw, *_: original(raw, NOW, 1)
    hub.run()
    assert [value["channel"] for value in sent] == ["heartbeats", "ticker", "market_trades"]
    assert all(
        set(value) == {"type", "product_ids", "channel"} and value["type"] == "subscribe"
        for value in sent
    )
    assert connect_args[0][0] == COINBASE_WS and "additional_headers" not in connect_args[0][1]
    assert connect_args[0][1]["max_queue"] == 16
    hub.close()


def test_wait_for_update_is_woken_by_market_event_and_has_no_queue_backlog():
    hub = StreamHub(autostart=False)
    old = hub.sequence
    hub.process_coinbase(ticker(), NOW, 1)
    assert hub.wait_for_update(old, timeout=0) > old
    assert hub.wait_for_update(hub.sequence, timeout=0) == hub.sequence
    assert not hub.thread

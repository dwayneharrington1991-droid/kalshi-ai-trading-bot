"""Offline public-flow replay; no account credentials or network."""
from datetime import timedelta
from decimal import Decimal
import zlib
import pytest
from src.signals_display.order_flow import Flow, Book
from tests.test_signals_display import NOW


def test_cvd_aggressor_sign_duplicates_staleness_and_reconnect_reset():
    f = Flow("BTC")
    for i in range(61):
        at = NOW + timedelta(seconds=i)
        f.trade(str(i), at, 100000, 1, "sell" if i % 5 == 0 else "buy", at)
    s = f.snapshot(NOW + timedelta(seconds=60))
    assert s["status"] == "LIVE" and s["cvd_since_reconnect"] == 35
    assert s["hidden_bullish_accumulation_candidate"] and s["probability_adjustment"] == 0
    f.trade("60", NOW + timedelta(seconds=60), 100000, 100, "buy", NOW + timedelta(seconds=60))
    assert f.cumulative == 35
    assert f.snapshot(NOW + timedelta(seconds=66))["delta_60s"] is None
    f.reset()
    assert f.snapshot(NOW + timedelta(seconds=66))["cvd_since_reconnect"] is None


def test_reject_unknown_aggressor_and_future_or_out_of_order_trade():
    f = Flow("BTC")
    for side, at in [
        ("maker", NOW),
        ("buy", NOW + timedelta(seconds=1)),
        ("sell", NOW - timedelta(seconds=6)),
    ]:
        with pytest.raises(ValueError):
            f.trade("x", at, 1, 1, side, NOW)
    f.trade("a", NOW, 1, 1, "buy", NOW)
    with pytest.raises(ValueError):
        f.trade("b", NOW - timedelta(seconds=1), 1, 1, "buy", NOW)


def packet(bids, asks, at=NOW):
    def digits(v):
        return format(Decimal(str(v)), "f").replace(".", "").lstrip("0")

    raw = "".join(
        digits(p) + digits(q)
        for levels, reverse in ((asks, False), (bids, True))
        for p, q in sorted(levels, reverse=reverse)[:10]
    )
    return {
        "bids": [{"price": p, "qty": q} for p, q in bids],
        "asks": [{"price": p, "qty": q} for p, q in asks],
        "timestamp": at.isoformat(),
        "checksum": zlib.crc32(raw.encode()),
    }


def test_book_top20_checksum_deletion_and_no_probability_bonus():
    bids = [(100 - i, "3.000") for i in range(25)]
    asks = [(101 + i, "1.000") for i in range(25)]
    b = Book()
    b.update(packet(bids, asks), True, NOW)
    s = b.snapshot(NOW)
    assert s["top_levels"] == 20 and s["bid_ask_ratio"] == 3 and s["dominant_side"] == "UP"
    assert s["probability_adjustment"] == 0
    bad = packet(bids, asks)
    bad["checksum"] += 1
    with pytest.raises(ValueError, match="checksum"):
        b.update(bad, False, NOW)
    assert b.snapshot(NOW)["bid_ask_ratio"] is None
    b.update(packet(bids, asks), True, NOW)
    assert b.snapshot(NOW + timedelta(seconds=6))["bid_ask_ratio"] is None
    assert Book().snapshot(NOW)["status"] == "UNAVAILABLE"


def test_book_requires_full20levels_and_snapshot_before_updates():
    b = Book()
    d = packet([(100, "1")], [(101, "1")])
    with pytest.raises(ValueError, match="snapshot"):
        b.update(d, False, NOW)
    b.update(d, True, NOW)
    assert b.snapshot(NOW)["bid_ask_ratio"] is None


def test_divergence_uses_confirmed_pivots_not_future_bars():
    f = Flow("BTC")
    # Peak 101 then peak 102, with lower CVD at the second peak.
    prices = [100, 101, 100, 100, 102, 100, 100]
    for minute, price in enumerate(prices):
        for second in range(60):
            at = NOW + timedelta(minutes=minute, seconds=second)
            side = "buy" if minute < 2 else "sell"
            f.trade(f"{minute}-{second}", at, price, 1, side, at)
        if minute == 4:
            assert not f.snapshot(at)["bearish_divergence_candidate"]
    assert f.snapshot(at)["bearish_divergence_candidate"]


def test_live_features_are_timestamp_gated_and_scanner_keeps_probability_separate():
    from src.signals_display.features import extract
    from src.signals_display.scanner import context
    from tests.test_signals_monitor import frame

    p = frame()
    data = {
        "spot": {"status": "LIVE", "observed_at": NOW.isoformat(), "delta_fraction_60s": 0.6},
        "perpetual": {
            "status": "LIVE",
            "observed_at": (NOW - timedelta(seconds=6)).isoformat(),
            "delta_fraction_60s": 0.9,
        },
        "spot_book": {"status": "LIVE", "observed_at": NOW.isoformat(), "bid_ask_ratio": 3},
    }
    p["coverage"] = {"Kraken public order flow": {"data": data}}
    f = extract(p, p["markets"][0], NOW)
    assert f["values"]["spot_cvd_delta_fraction"] == 0.6
    assert f["values"]["perpetual_cvd_delta_fraction"] is None
    assert f["values"]["spot_book_dominance"] == 0.5
    scan = context(p, p["markets"][0], {}, NOW)
    assert scan["order_flow"] == data and scan["confluence_probability"] is None


def test_perpetual_subscription_ack_is_not_a_trade_and_live_updates_are_consumed(monkeypatch):
    import json
    import threading
    from websockets.sync import client
    from src.signals_display.order_flow import PublicOrderFlow

    engine = object.__new__(PublicOrderFlow)
    engine.lock = threading.Lock()
    engine.stop = threading.Event()
    engine.spot = Flow("BTC")
    engine.perp = Flow("raw")
    engine.book = Book()
    engine.errors = {}
    sent = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def send(self, data):
            sent.append(json.loads(data))

        def recv(self, timeout):
            if not getattr(self, "acked", False):
                self.acked = True
                return json.dumps(
                    {"event": "subscribed", "feed": "trade", "product_ids": ["PF_XBTUSD"]}
                )
            engine.stop.set()
            return json.dumps(
                {
                    "feed": "trade",
                    "product_id": "PF_XBTUSD",
                    "seq": 1,
                    "uid": "public-mock",
                    "time": int(NOW.timestamp() * 1000),
                    "price": 100000,
                    "qty": 0.2,
                    "side": "buy",
                }
            )

    monkeypatch.setattr(client, "connect", lambda *a, **k: Socket())
    monkeypatch.setattr("src.signals_display.order_flow.utcnow", lambda: NOW)
    engine.run("perpetual")
    assert engine.perp.cumulative == 0.2 and engine.errors["perpetual"] is None
    assert sent == [{"event": "subscribe", "feed": "trade", "product_ids": ["PF_XBTUSD"]}]

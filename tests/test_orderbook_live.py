"""Live order-book logic: ordering, quantities, cumulative depth, walls, checksum, sequence gaps, duplicates, staleness."""
import time
import zlib
from decimal import Decimal as D

from src.signals_display import orderbook_live as ob


def _book(bids, asks, depth=None):
    b = ob.Book("test", depth=depth)
    for p, q in bids: b.apply("bid", D(p), D(q))
    for p, q in asks: b.apply("ask", D(p), D(q))
    b.status, b.last_event, b.last_receipt = "OK", time.time(), time.time()
    return b


def test_ladder_ordering_quantities_and_cumulative_depth():
    b = _book([("100.0", "1"), ("99.5", "2"), ("99.0", "0.5")], [("100.5", "1.5"), ("101.0", "1"), ("102.0", "4")])
    v, _ = ob.depth_view(b)
    assert [x[0] for x in v["bids"]] == [100.0, 99.5, 99.0] and [x[0] for x in v["asks"]] == [100.5, 101.0, 102.0]
    assert [x[2] for x in v["bids"]] == [1.0, 3.0, 3.5] and [x[2] for x in v["asks"]] == [1.5, 2.5, 6.5]
    assert v["spread"] == 0.5 and v["mid"] == 100.25 and v["status"] == "LIVE"


def test_zero_size_removes_level_and_depth_truncates():
    b = _book([("100", "1"), ("99", "1"), ("98", "1")], [("101", "1")], depth=2)
    b.apply("bid", D("99"), D("0")); b.truncate()
    assert sorted(b.bids) == [D("98"), D("100")]
    b.apply("bid", D("97"), D("1")); b.apply("bid", D("96"), D("1")); b.truncate()
    assert sorted(b.bids) == [D("98"), D("100")]


def test_walls_and_wall_events_with_trade_consumption():
    bids = [(f"{100 - i}", "1") for i in range(10)] + [("89", "10")]
    asks = [(f"{101 + i}", "1") for i in range(10)] + [("112", "8")]
    b = _book(bids, asks)
    v, walls = ob.depth_view(b, prev_walls={})
    assert {w["price"] for w in v["walls"]} == {89.0, 112.0} and all(e["event"] == "NEW" for e in v["wall_events"])
    flow = ob.TradeFlow(); now = time.time()
    flow.add(now, "sell", 9.0, 89.0, trade_id="t1")                       # sellers hit the 89 bid wall
    b.apply("bid", D("89"), D("0")); b.apply("ask", D("112"), D("0"))     # ask wall pulled without trades
    v2, _ = ob.depth_view(b, now=now, prev_walls=walls, recent_trades=flow)
    gone = {e["wall"]: e["classification"] for e in v2["wall_events"] if e["event"] == "GONE"}
    assert gone["bid:89"] == "CONSUMED BY TRADES" and gone["ask:112"].startswith("PULLED")


def test_kraken_checksum_matches_reference_construction():
    b = _book([("60000.1", "0.5"), ("60000.0", "1.25")], [("60000.2", "0.01"), ("60000.5", "2")])
    s = "600002" + "1000000" + "600005" + "200000000" + "600001" + "50000000" + "600000" + "125000000"
    assert ob.kraken_checksum(b) == zlib.crc32(s.encode()) & 0xFFFFFFFF


def test_sequence_gap_detected():
    s = ob.SequenceTracker()
    assert s.ok(10) and s.ok(11) and not s.ok(13) and s.ok(14)


def test_duplicate_trades_ignored_and_cvd_windows():
    f = ob.TradeFlow(); now = time.time()
    assert f.add(now - 2, "buy", 2.0, 100.0, "a") and not f.add(now - 2, "buy", 2.0, 100.0, "a")
    f.add(now - 0.5, "sell", 0.5, 100.0, "b"); f.add(now - 100, "sell", 1.0, 100.0, "c")
    w = f.windows(now)
    assert w["1s"]["sell_btc"] == 0.5 and w["5s"]["buy_btc"] == 2.0 and w["300s"]["trades"] == 3
    assert f.cvd == 0.5 and w["5s"]["large_trades"] == 1


def test_stale_crossed_and_resync_books_are_never_live():
    b = _book([("100", "1")], [("101", "1")])
    assert b.freshness() == "LIVE"
    b.last_event = time.time() - ob.STALE_S - 1
    assert b.freshness() == "STALE"
    c = _book([("102", "1")], [("101", "1")])
    assert c.freshness() == "RESYNC"                                      # crossed book = corrupt
    c.reset("RESYNC")
    assert ob.depth_view(c)[0]["status"] == "RESYNC"


def test_pressure_label_is_descriptive_and_needs_live_book():
    b = _book([("100", "5")], [("100.1", "1")])
    v, _ = ob.depth_view(b)
    assert ob.pressure(v, {"60s": {"imbalance": 0.5}})["label"] == "BUY"
    assert ob.pressure(v, {"60s": {"imbalance": -0.5}})["label"] == "MIXED"
    assert ob.pressure({"status": "STALE"}, {})["label"] == "UNAVAILABLE"


def test_only_the_largest_walls_are_flagged():
    bids = [(f"{100 - i}", "1") for i in range(15)] + [("84", "6"), ("83", "7"), ("82", "8"), ("81", "9")]
    v, _ = ob.depth_view(_book(bids, [("101", "1"), ("102", "1")]))
    assert sorted(w["price"] for w in v["walls"] if w["side"] == "bid") == [81.0, 82.0, 83.0]   # 84 (6 BTC) not top 3


def test_api_body_marks_old_snapshot_stale(tmp_path):
    import json
    f = tmp_path / "ob.json"
    f.write_text(json.dumps({"generated_at": 1000.0, "venues": {"kraken": {"status": "LIVE"}}}))
    stale = json.loads(ob.latest_body(f, now=1010.0))
    assert stale["status"] == "STALE" and stale["venues"]["kraken"]["status"] == "STALE"
    assert json.loads(ob.latest_body(f, now=1001.0))["venues"]["kraken"]["status"] == "LIVE"
    assert json.loads(ob.latest_body(tmp_path / "none.json"))["status"] == "UNAVAILABLE"

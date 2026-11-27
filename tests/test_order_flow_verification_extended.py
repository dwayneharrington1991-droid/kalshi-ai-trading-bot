"""Adversarial checks for the offline spot order-flow verifier.

These fixtures validate arithmetic and fail-closed timing semantics only.  They
do not stand in for a raw exchange capture, and a displayed-depth reduction is
deliberately not labelled as an execution or cancellation.
"""

from decimal import Decimal

import pytest

from src.signals_display.order_flow_verification import (
    Trade,
    book_second,
    spot_depth,
    trade_windows,
    wall_changes,
)


def _twenty_level_book(*, bid_wall=Decimal("12"), ask_wall=Decimal("15")):
    bids = [(Decimal("100") - i / Decimal("10"), Decimal("1")) for i in range(1, 21)]
    asks = [(Decimal("100") + i / Decimal("10"), Decimal("1")) for i in range(1, 21)]
    bids[9] = (bids[9][0], bid_wall)
    asks[14] = (asks[14][0], ask_wall)
    return bids, asks


def test_twenty_level_cumulative_depth_and_wall_lifecycle_are_exact():
    bids, asks = _twenty_level_book()
    initial = spot_depth(bids, asks)

    assert len(initial["bids"]) == len(initial["asks"]) == 20
    assert initial["bids"][4][2] == Decimal("5")       # top five
    assert initial["bids"][9][2] == Decimal("21")      # top ten, including the wall
    assert initial["bids"][19][2] == Decimal("31")     # top twenty
    assert initial["asks"][9][2] == Decimal("10")
    assert initial["asks"][19][2] == Decimal("34")

    grown_bids, _ = _twenty_level_book(bid_wall=Decimal("18"))
    grown = spot_depth(grown_bids, asks)
    assert wall_changes(initial, grown) == [
        ("bids", Decimal("99"), Decimal("12"), Decimal("18"), True, True)
    ]

    shrunk_bids, _ = _twenty_level_book(bid_wall=Decimal("4"))
    shrunk = spot_depth(shrunk_bids, asks)
    assert wall_changes(grown, shrunk) == [
        ("bids", Decimal("99"), Decimal("18"), Decimal("4"), True, True)
    ]
    assert wall_changes(shrunk, grown) == [
        ("bids", Decimal("99"), Decimal("4"), Decimal("18"), True, True)
    ]

    removed_bids = [level for level in shrunk_bids if level[0] != Decimal("99")]
    removed = spot_depth(removed_bids, asks)
    assert wall_changes(shrunk, removed) == [
        ("bids", Decimal("99"), Decimal("4"), Decimal("0"), True, False)
    ]


def test_replayed_duplicate_trade_is_counted_once_and_conflicts_fail_closed():
    original = Trade("cb-7", 100.1, 100.2, "buy", Decimal("0.7"))
    replay = Trade("cb-7", 100.1, 100.8, "buy", Decimal("0.7"))
    delayed = Trade("cb-8", 100.2, 101.1, "sell", Decimal("0.2"))

    at_101 = trade_windows([original, replay, delayed], 101)
    assert at_101[1]["count"] == 1
    assert at_101[1]["cvd_change_btc"] == Decimal("0.7")

    at_102 = trade_windows([original, replay, delayed], 102)
    assert at_102[5]["count"] == 2
    assert at_102[5]["cvd_change_btc"] == Decimal("0.5")

    with pytest.raises(ValueError, match="conflicting trade ID"):
        trade_windows(
            [original, Trade("cb-7", 100.1, 100.8, "sell", Decimal("0.7"))],
            101,
        )


def test_book_freshness_uses_channel_integrity_receipt_not_other_traffic():
    base = {
        "last_change": 100.2,
        "last_integrity_receipt": 100.4,
        "max_age": 2,
        "connected": True,
        "synced": True,
    }
    assert book_second(cutoff=101, **base) == "UPDATED"
    assert book_second(cutoff=102, **base) == "UNCHANGED"
    assert book_second(cutoff=103, **base) == "STALE"

    # A heartbeat from another channel must not be substituted for a book
    # integrity receipt.  Passing a future receipt fails closed.
    assert book_second(cutoff=101, **(base | {"last_integrity_receipt": 101.1})) == "UNAVAILABLE"
    assert book_second(cutoff=101, **(base | {"synced": False})) == "UNAVAILABLE"

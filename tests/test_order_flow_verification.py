from decimal import Decimal

import pytest

from src.signals_display.order_flow_verification import (
    Trade,
    book_second,
    spot_depth,
    trade_windows,
    wall_changes,
)


# Unsorted normalized spot snapshots, with a known bid wall at 99.
BIDS = [(96, 1), (99, 9), (97, 1), (98, 1), (95, 1)]
ASKS = [(104, 1), (101, 2), (102, 1), (105, 1), (103, 1)]


def test_exact_depth_ladders_and_wall_disappearance():
    snapshot = spot_depth(BIDS, ASKS)
    assert [r[0] for r in snapshot["bids"]] == [99, 98, 97, 96, 95]
    assert [r[0] for r in snapshot["asks"]] == [101, 102, 103, 104, 105]
    assert snapshot["bids"][0] == (99, 9, 9, True)
    assert snapshot["bids"][-1][2] == 13
    assert snapshot["asks"][-1][2] == 6
    assert snapshot["spread"] == 2
    assert snapshot["midpoint"] == 100
    assert snapshot["top_imbalance"] == Decimal(7) / 11
    assert snapshot["depth_imbalance"] == Decimal(7) / 19
    assert snapshot["band_imbalance"][25] is None
    reduced = spot_depth([(99, 4), *(row for row in BIDS if row[0] != 99)], ASKS)
    removed = spot_depth([row for row in BIDS if row[0] != 99], ASKS)
    assert wall_changes(snapshot, reduced) == [("bids", 99, 9, 4, True, True)]
    assert wall_changes(reduced, removed) == [("bids", 99, 4, 0, True, False)]
    assert wall_changes(removed, reduced) == [("bids", 99, 0, 4, False, True)]


@pytest.mark.parametrize("bids", [[(101, 1)], [(99, 0)], [(99, 1), (99, 2)], [(99, "NaN")]])
def test_invalid_spot_book_is_not_published(bids):
    with pytest.raises(ValueError):
        spot_depth(bids, ASKS)


def test_one_second_preserves_all_trades_and_ignores_future_receipts():
    buy = Trade("1", 99.1, 99.2, "buy", Decimal("0.45"))
    sell = Trade("2", 99.8, 99.9, "sell", Decimal("0.10"))
    late = Trade("3", 99.9, 100.1, "buy", Decimal("0.99"))
    boundary = Trade("4", 99, 99, "sell", Decimal("0.20"))
    retry = Trade("1", 99.1, 99.5, "buy", Decimal("0.45"))
    windows = trade_windows([buy, retry, sell, late, boundary], 100)
    assert windows[1] == {
        "buy_btc": Decimal("0.45"),
        "sell_btc": Decimal("0.10"),
        "cvd_change_btc": Decimal("0.35"),
        "count": 2,
        "trades_per_second": 2,
    }
    for window in (5, 15, 30, 60, 300):
        assert windows[window]["cvd_change_btc"] == Decimal("0.15")
    assert trade_windows([buy, sell, late], 101)[1]["count"] == 0
    assert trade_windows([buy, sell, late], 101)[5]["count"] == 3


def test_trade_conflict_or_impossible_clock_fails_closed():
    trade = Trade("1", 99, 99, "buy", Decimal("0.5"))
    with pytest.raises(ValueError, match="conflicting"):
        trade_windows([trade, Trade("1", 99, 99, "sell", trade.btc)], 100)
    with pytest.raises(ValueError, match="event time"):
        trade_windows([Trade("2", 100, 99, "buy", trade.btc)], 100)


def test_book_clock_distinguishes_quiet_stale_and_disconnected():
    kwargs = dict(last_change=99.9, last_integrity_receipt=100, max_age=2, connected=True,
                  synced=True)
    assert book_second(cutoff=100, **kwargs) == "UPDATED"
    assert book_second(cutoff=101, **kwargs) == "UNCHANGED"
    assert book_second(cutoff=103, **kwargs) == "STALE"
    assert book_second(cutoff=99, **kwargs) == "UNAVAILABLE"
    assert book_second(cutoff=100, **(kwargs | {"connected": False})) == "UNAVAILABLE"
    assert book_second(cutoff=100, **(kwargs | {"synced": False})) == "UNAVAILABLE"

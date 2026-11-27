"""Independent checks for normalized, receipt-ordered spot feed captures.

Run per venue/symbol. Exchange-specific reconstruction and integrity checks
belong in the adapters that provide these inputs.
"""

from dataclasses import dataclass
from decimal import Decimal
from itertools import accumulate
from math import isfinite
from statistics import median


WINDOWS = (5, 15, 30, 60, 300)
ZERO = Decimal(0)


def _decimal(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("expected a finite nonnegative number")
    return number


def _ratio(bid, ask):
    return (bid - ask) / (bid + ask) if bid + ask else None


def _ladder(levels, *, descending):
    by_price = {}
    for raw_price, raw_quantity in levels:
        price, quantity = _decimal(raw_price), _decimal(raw_quantity)
        if not price or not quantity or price in by_price:
            raise ValueError("snapshot requires unique positive price and quantity levels")
        by_price[price] = quantity
    ordered = sorted(by_price.items(), reverse=descending)
    if not ordered:
        raise ValueError("empty side")
    typical = median(by_price.values())
    return [
        (price, quantity, cumulative, len(ordered) >= 5 and quantity >= 3 * typical)
        for (price, quantity), cumulative in zip(
            ordered, accumulate(quantity for _, quantity in ordered)
        )
    ]


def spot_depth(bids, asks):
    """Return (price, BTC, cumulative BTC, wall) ladders from a complete spot snapshot."""
    bid, ask = _ladder(bids, descending=True), _ladder(asks, descending=False)
    best_bid, best_ask = bid[0][0], ask[0][0]
    if best_bid >= best_ask:
        raise ValueError("locked or crossed spot book")
    midpoint = (best_bid + best_ask) / 2
    return {
        "bids": bid,
        "asks": ask,
        "midpoint": midpoint,
        "spread": best_ask - best_bid,
        "top_imbalance": _ratio(bid[0][1], ask[0][1]),
        "depth_imbalance": _ratio(bid[-1][2], ask[-1][2]),
        "band_imbalance": {
            bps: _ratio(
                *(sum((qty for price, qty, _, _ in side
                       if abs(price - midpoint) * 10000 <= midpoint * bps), ZERO)
                  for side in (bid, ask))
            )
            for bps in (5, 10, 25)
        },
    }


def wall_changes(previous, current):
    """Compare adjacent intact ladders covering the same price range.

    A reduction measures displayed depth. Attribution needs separate evidence.
    The caller must exclude depth-boundary evictions.
    """
    changes = []
    for side in ("bids", "asks"):
        before = {price: (qty, wall) for price, qty, _, wall in previous[side]}
        after = {price: (qty, wall) for price, qty, _, wall in current[side]}
        for price in sorted(before.keys() | after.keys(), reverse=side == "bids"):
            old, was_wall = before.get(price, (ZERO, False))
            new, is_wall = after.get(price, (ZERO, False))
            if (was_wall or is_wall) and (old != new or was_wall != is_wall):
                changes.append((side, price, old, new, was_wall, is_wall))
    return changes


@dataclass(frozen=True)
class Trade:
    trade_id: str
    event_at: float
    received_at: float
    aggressor: str
    btc: Decimal


def trade_windows(trades, cutoff):
    """Audit one venue's trades over (cutoff - window, cutoff].

    Coverage must be verified by the caller. A conflicting observed ID prevents
    using the batch. Future receipts are invisible at the decision cutoff.
    """
    if not isfinite(cutoff):
        raise ValueError("invalid cutoff")
    seen = {}
    for trade in trades:
        if not isfinite(trade.received_at):
            raise ValueError("invalid receipt time")
        if trade.received_at > cutoff:
            continue
        if not isfinite(trade.event_at) or trade.event_at > trade.received_at:
            raise ValueError("invalid trade event time")
        if trade.event_at <= cutoff - max(WINDOWS):
            continue
        quantity = _decimal(trade.btc)
        if not trade.trade_id or trade.aggressor not in ("buy", "sell") or not quantity:
            raise ValueError("invalid identified trade")
        record = (trade.event_at, trade.aggressor, quantity)
        if seen.setdefault(trade.trade_id, record) != record:
            raise ValueError("conflicting trade ID")

    report = {}
    for window in (1, *WINDOWS):
        buy = sell = ZERO
        count = 0
        for event_at, side, quantity in seen.values():
            if event_at <= cutoff - window:
                continue
            count += 1
            if side == "buy":
                buy += quantity
            else:
                sell += quantity
        report[window] = {
            "buy_btc": buy, "sell_btc": sell, "cvd_change_btc": buy - sell,
            "count": count, "trades_per_second": count / window,
        }
    return report


def book_second(*, cutoff, last_change, last_integrity_receipt, max_age, connected, synced):
    """Classify the book as of an integer second using channel-scoped evidence."""
    if not connected or not synced or last_change is None or last_integrity_receipt is None:
        return "UNAVAILABLE"
    if not all(isfinite(value) for value in (cutoff, last_change, last_integrity_receipt, max_age)):
        return "UNAVAILABLE"
    if not last_change <= last_integrity_receipt <= cutoff:
        return "UNAVAILABLE"
    if cutoff - last_integrity_receipt > max_age:
        return "STALE"
    return "UPDATED" if cutoff - 1 < last_change else "UNCHANGED"

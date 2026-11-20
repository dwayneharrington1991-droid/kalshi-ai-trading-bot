"""Venue-neutral L2/trade-flow state with no network or execution capability."""
from __future__ import annotations

from collections import deque
from decimal import Decimal

from .contract_rules import decimal_value


class SequenceGap(ValueError):
    pass


class NormalizedFlow:
    def __init__(self, venue, symbol, *, maximum_levels=200, maximum_trades=5000):
        if not 1 <= maximum_levels <= 1000 or not 10 <= maximum_trades <= 100000:
            raise ValueError("resource limits outside supported bounds")
        self.venue, self.symbol = venue, symbol
        self.maximum_levels = maximum_levels
        self.bids, self.asks = {}, {}
        self.sequence = None
        self.observed_at = None
        self.event_at = None
        self.valid = False
        self.trades = deque(maxlen=maximum_trades)
        self.cvd = Decimal(0)
        self.previous_depth = None
        self.diagnostics = {"sequence_gaps": 0, "dropped_messages": 0, "updates": 0}

    @staticmethod
    def _side(levels, *, reverse):
        output = {}
        for price, size in levels:
            p = decimal_value(price, low=Decimal("0.00000001"))
            q = decimal_value(size, low=Decimal(0))
            if q:
                output[p] = q
        return dict(sorted(output.items(), reverse=reverse))

    def snapshot(self, *, bids, asks, sequence, event_at, observed_at):
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("integer sequence required")
        self.bids = dict(list(self._side(bids, reverse=True).items())[:self.maximum_levels])
        self.asks = dict(list(self._side(asks, reverse=False).items())[:self.maximum_levels])
        if not self.bids or not self.asks or next(iter(self.bids)) >= next(iter(self.asks)):
            raise ValueError("empty or crossed order book")
        self.sequence, self.event_at, self.observed_at, self.valid = sequence, event_at, observed_at, True
        self.previous_depth = self._depth()

    def delta(self, *, side, price, size, sequence, event_at, observed_at):
        if not self.valid or sequence != self.sequence + 1:
            self.valid = False
            self.diagnostics["sequence_gaps"] += 1
            raise SequenceGap("order-book sequence gap; new snapshot required")
        destination = self.bids if side == "bid" else self.asks if side == "ask" else None
        if destination is None:
            raise ValueError("side must be bid or ask")
        p, q = decimal_value(price, low=Decimal("0.00000001")), decimal_value(size, low=Decimal(0))
        if q:
            destination[p] = q
        else:
            destination.pop(p, None)
        destination_sorted = sorted(destination.items(), reverse=side == "bid")[:self.maximum_levels]
        destination.clear(); destination.update(destination_sorted)
        if not self.bids or not self.asks or next(iter(self.bids)) >= next(iter(self.asks)):
            self.valid = False
            raise ValueError("delta produced empty or crossed book")
        self.sequence, self.event_at, self.observed_at = sequence, event_at, observed_at
        self.diagnostics["updates"] += 1

    def trade(self, *, price, size, aggressor, event_at, observed_at):
        p = decimal_value(price, low=Decimal("0.00000001"))
        q = decimal_value(size, low=Decimal("0.00000001"))
        if aggressor not in {"buy", "sell"}:
            raise ValueError("aggressor must be buy or sell")
        signed = q if aggressor == "buy" else -q
        before = self.cvd
        self.cvd += signed
        self.trades.append((p, q, aggressor, event_at, observed_at, self.cvd - before))

    def _depth(self, levels=20):
        return (sum(list(self.bids.values())[:levels], Decimal(0)),
                sum(list(self.asks.values())[:levels], Decimal(0)))

    def metrics(self, *, levels=20, bucket_volume=Decimal("10")):
        if not self.valid:
            return {"status": "STALE_OR_DISCONNECTED", "venue": self.venue, "symbol": self.symbol,
                    "diagnostics": dict(self.diagnostics)}
        bid_depth, ask_depth = self._depth(levels)
        total = bid_depth + ask_depth
        current = (bid_depth, ask_depth)
        prior = self.previous_depth or current
        replenishment = (max(Decimal(0), current[0] - prior[0]) +
                         max(Decimal(0), current[1] - prior[1]))
        cancellation = (max(Decimal(0), prior[0] - current[0]) +
                        max(Decimal(0), prior[1] - current[1]))
        self.previous_depth = current
        recent = list(self.trades)
        buy = sum((row[1] for row in recent if row[2] == "buy"), Decimal(0))
        sell = sum((row[1] for row in recent if row[2] == "sell"), Decimal(0))
        halves = len(recent) // 2
        early = sum((row[5] for row in recent[:halves]), Decimal(0)) if halves else Decimal(0)
        late = sum((row[5] for row in recent[halves:]), Decimal(0)) if recent else Decimal(0)
        vpin = None
        if buy + sell >= bucket_volume and bucket_volume > 0:
            vpin = abs(buy - sell) / (buy + sell)
        return {
            "status": "LIVE", "venue": self.venue, "symbol": self.symbol,
            "sequence": self.sequence, "event_at": self.event_at, "observed_at": self.observed_at,
            "best_bid": str(next(iter(self.bids))), "best_ask": str(next(iter(self.asks))),
            "spread": str(next(iter(self.asks)) - next(iter(self.bids))),
            "bid_depth": str(bid_depth), "ask_depth": str(ask_depth),
            "depth_imbalance": str((bid_depth - ask_depth) / total) if total else None,
            "aggressive_buy_volume": str(buy), "aggressive_sell_volume": str(sell),
            "cvd": str(self.cvd), "order_flow_acceleration": str(late - early),
            "replenishment": str(replenishment), "cancellation": str(cancellation),
            "vpin_style_toxicity": str(vpin) if vpin is not None else None,
            "vpin_status": "AVAILABLE" if vpin is not None else "INSUFFICIENT_TRADE_VOLUME",
            "diagnostics": dict(self.diagnostics),
        }


def divergence(venue_metrics):
    valid = [row for row in venue_metrics if row.get("status") == "LIVE"]
    if len(valid) < 2:
        return {"status": "UNAVAILABLE", "reason": "At least two fresh compatible venues required"}
    mids = [(Decimal(row["best_bid"]) + Decimal(row["best_ask"])) / 2 for row in valid]
    return {"status": "AVAILABLE", "maximum_midpoint_divergence": str(max(mids) - min(mids)),
            "venues": [row["venue"] for row in valid], "predictive_effect": "UNVALIDATED"}


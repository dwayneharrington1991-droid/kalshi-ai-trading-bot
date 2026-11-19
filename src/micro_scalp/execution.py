"""Paper execution against observable evidence only.

An order decided at ``t`` reaches the simulated exchange at ``t + latency``
and lives for ``fill_window`` seconds (immediate-or-cancel). It can fill only
against evidence that appears at or after arrival:

* depth mode (live recordings): the first order-book snapshot received after
  arrival. Levels are walked up to the limit; each level contributes at most
  ``liquidity_fraction`` of its displayed size (queue competition / hidden
  races are unknown). Liquidity we consume is remembered for a few seconds so
  later orders cannot re-use it.
* print mode (historical, no published depth): real trade prints after
  arrival on the side that proves the price was executable (a taker-YES print
  at p shows a YES ask at p). Each print contributes at most
  ``liquidity_fraction`` of its size, and each print is consumed once.

Fees are charged per simulated execution with Kalshi's taker formula. An
order with no fill is recorded as UNFILLED and is never counted as a trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import itertools

from .risk import taker_fee

_ids = itertools.count(1)


@dataclass(frozen=True)
class ExecutionConfig:
    latency_seconds: float = 1.0
    fill_window_seconds: float = 2.0
    limit_tolerance: float = 0.01  # max price worse than the decision quote
    print_mode_extra_slippage: float = 0.01  # added to every print-evidence fill
    depth_mode_extra_slippage: float = 0.0  # depth walk already prices impact
    liquidity_fraction: float = 0.25
    max_book_age_seconds: float = 3.0
    consumed_memory_seconds: float = 5.0
    fee_multiplier: float = 1.0


@dataclass
class Order:
    ticker: str
    side: str  # "up" | "down"
    action: str  # "buy" | "sell"
    quantity: int
    limit: float
    decided_at: float
    arrival: float
    expires: float
    reason: str
    id: int = field(default_factory=lambda: next(_ids))
    filled: float = 0.0
    fills: list = field(default_factory=list)
    status: str = "PENDING"
    real: bool = False  # always False; checked by the paper gateway

    @property
    def remaining(self):
        return self.quantity - self.filled

    def vwap(self):
        q = sum(f["qty"] for f in self.fills)
        return sum(f["qty"] * f["price"] for f in self.fills) / q if q else None

    def fees(self):
        return sum(f["fee"] for f in self.fills)

    def to_dict(self):
        return {
            "id": self.id,
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "quantity": self.quantity,
            "limit": round(self.limit, 4),
            "decided_at": self.decided_at,
            "arrival": self.arrival,
            "status": self.status,
            "filled": self.filled,
            "vwap": self.vwap(),
            "fees": round(self.fees(), 4),
            "reason": self.reason,
            "fills": self.fills,
        }


class PaperExchange:
    def __init__(self, config: ExecutionConfig):
        self.c = config
        self.pending: list[Order] = []
        self.done: list[Order] = []
        self.consumed = {}  # (ticker, book_key, price) -> (qty, until)
        self.print_used = {}  # id(print event) -> qty used

    def make_order(self, ticker, side, action, qty, quote_price, t, reason):
        tol = self.c.limit_tolerance
        limit = min(0.99, quote_price + tol) if action == "buy" else max(0.01, quote_price - tol)
        arrival = t + self.c.latency_seconds
        return Order(ticker, side, action, int(qty), limit, t, arrival, arrival + self.c.fill_window_seconds, reason)

    def submit(self, order: Order):
        self.pending.append(order)

    def _finish(self, order, status=None):
        order.status = status or ("FILLED" if order.remaining <= 1e-9 else ("PARTIAL" if order.filled > 0 else "UNFILLED"))
        self.pending.remove(order)
        self.done.append(order)
        return order

    def expire(self, t):
        """Close orders whose window ended before ``t``. Returns finished orders."""
        out = []
        for o in list(self.pending):
            if t > o.expires:
                out.append(self._finish(o))
        return out

    def cancel_ticker(self, ticker, reason="CANCELLED_MARKET_CLOSED"):
        out = []
        for o in list(self.pending):
            if o.ticker == ticker:
                out.append(self._finish(o, reason if o.filled == 0 else "PARTIAL"))
        return out

    def _fill(self, order, price, qty, t, evidence):
        slip = self.c.depth_mode_extra_slippage if evidence == "orderbook" else self.c.print_mode_extra_slippage
        px = min(0.99, price + slip) if order.action == "buy" else max(0.01, price - slip)
        fee = taker_fee(px, qty, self.c.fee_multiplier)
        order.filled += qty
        order.fills.append({"t": t, "price": round(px, 4), "evidence_price": price, "qty": qty, "fee": fee, "evidence": evidence})

    def on_quote(self, e, has_depth):
        """Depth-mode fills from the first snapshot at/after arrival."""
        finished = []
        if e.get("depth") is None:
            return finished
        for o in list(self.pending):
            if o.ticker != e["ticker"] or e["at"] < o.arrival or e["at"] > o.expires:
                continue
            key = f"{o.side}_{'asks' if o.action == 'buy' else 'bids'}"
            for price, qty in e["depth"].get(key) or []:
                ok = price <= o.limit + 1e-9 if o.action == "buy" else price >= o.limit - 1e-9
                if not ok or o.remaining <= 0:
                    break
                ck = (o.ticker, key, price)
                used, until = self.consumed.get(ck, (0.0, 0.0))
                if e["at"] > until:
                    used = 0.0
                avail = int(max(0.0, qty * self.c.liquidity_fraction - used))
                take = min(o.remaining, avail)
                if take >= 1:
                    self._fill(o, price, take, e["at"], "orderbook")
                    self.consumed[ck] = (used + take, e["at"] + self.c.consumed_memory_seconds)
            # IOC: one snapshot after arrival decides the order.
            finished.append(self._finish(o))
        return finished

    def on_trade(self, e, has_depth):
        """Print-mode fills (only for contracts without published depth)."""
        finished = []
        if has_depth:
            return finished
        for o in list(self.pending):
            if o.ticker != e["ticker"] or e["exchange_time"] < o.arrival or e["exchange_time"] > o.expires:
                continue
            yes = e["yes_price"]
            # Which prints prove our price was executable:
            if o.side == "up" and o.action == "buy" and e["taker_side"] == "yes":
                price = yes
            elif o.side == "up" and o.action == "sell" and e["taker_side"] == "no":
                price = yes
            elif o.side == "down" and o.action == "buy" and e["taker_side"] == "no":
                price = round(1 - yes, 6)
            elif o.side == "down" and o.action == "sell" and e["taker_side"] == "yes":
                price = round(1 - yes, 6)
            else:
                continue
            ok = price <= o.limit + 1e-9 if o.action == "buy" else price >= o.limit - 1e-9
            if not ok:
                continue
            key = id(e)
            avail = int(e["count"] * self.c.liquidity_fraction - self.print_used.get(key, 0))
            take = min(o.remaining, avail)
            if take >= 1:
                self._fill(o, price, take, e["exchange_time"], "trade_print")
                self.print_used[key] = self.print_used.get(key, 0) + take
            if o.remaining <= 1e-9:
                finished.append(self._finish(o))
        return finished

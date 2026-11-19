"""Per-contract market state and causal features.

State is updated only by events already available. Features at time ``t`` use
only state as of ``t``. Missing inputs produce explicit zero values plus an
availability indicator; they are never imputed from the future.
"""
from __future__ import annotations

from collections import deque
import math

FEATURE_NAMES_MARKET = (
    "spread",
    "mid_dev",
    "mid_dev_x_elapsed",
    "time_left_frac",
    "ret_10s",
    "ret_30s",
    "ret_60s",
    "realized_vol_60s",
    "flow_imbalance_30s",
    "flow_imbalance_60s",
    "log_volume_60s",
    "book_imbalance",
    "book_available",
)
# Existing AI signals. LIVE and OPENING stay separate inputs.
FEATURE_NAMES_AI = ("ai_live_minus_market", "ai_live_available", "ai_opening_minus_half", "ai_opening_available")


class MarketState:
    def __init__(self, ticker, opens, closes, strike=None):
        self.ticker, self.opens, self.closes, self.strike = ticker, opens, closes, strike
        self.up_bid = self.up_ask = None
        self.bid_at = self.ask_at = -math.inf
        self.depth = None
        self.depth_at = -math.inf
        self.has_depth_source = False
        self.mids = deque()  # (t, mid) on each change
        self.flow = deque()  # (t, signed_count, count)
        self.ai_live_up, self.ai_live_at = None, -math.inf
        self.ai_opening_up = None
        self.outcome = None

    # ---- updates -------------------------------------------------------
    def _record_mid(self, t):
        if self.up_bid is not None and self.up_ask is not None and self.up_bid < self.up_ask:
            mid = (self.up_bid + self.up_ask) / 2
            if not self.mids or self.mids[-1][1] != mid:
                self.mids.append((t, mid))
        # Keep the newest point older than the window so lookbacks still resolve
        # when the price has been unchanged for a long time.
        while len(self.mids) > 1 and self.mids[1][0] < t - 180:
            self.mids.popleft()

    def on_quote(self, e):
        if e.get("depth") is not None:
            self.has_depth_source = True
            self.depth, self.depth_at = e["depth"], e["at"]
        elif self.has_depth_source:
            return  # never mix inferred quotes into a depth-driven book
        self.up_bid, self.up_ask = e["up_bid"], e["up_ask"]
        self.bid_at = self.ask_at = e["at"]
        self._record_mid(e["at"])

    def on_trade(self, e):
        signed = e["count"] if e["taker_side"] == "yes" else -e["count"]
        self.flow.append((e["at"], signed, e["count"]))
        while self.flow and self.flow[0][0] < e["at"] - 120:
            self.flow.popleft()
        if self.has_depth_source:
            return
        # Without published depth, a print reveals one executable side:
        # taker YES lifted the YES ask; taker NO hit the YES bid.
        p, at = e["yes_price"], e["exchange_time"]
        if e["taker_side"] == "yes":
            self.up_ask, self.ask_at = p, at
            if self.up_bid is not None and self.up_bid >= p:
                self.bid_at = -math.inf  # contradicted; wait for fresh evidence
        else:
            self.up_bid, self.bid_at = p, at
            if self.up_ask is not None and self.up_ask <= p:
                self.ask_at = -math.inf
        self._record_mid(e["at"])

    def on_ai(self, e):
        if e.get("live_up") is not None:
            self.ai_live_up, self.ai_live_at = e["live_up"], e["at"]
        if e.get("opening_up") is not None and e.get("opening_ticker") in (None, self.ticker):
            self.ai_opening_up = e["opening_up"]

    # ---- views ---------------------------------------------------------
    def quote_age(self, t):
        return t - min(self.bid_at, self.ask_at)

    def quotes(self):
        if self.up_bid is None or self.up_ask is None:
            return None
        return {
            "up": {"bid": self.up_bid, "ask": self.up_ask},
            "down": {"bid": round(1 - self.up_ask, 6), "ask": round(1 - self.up_bid, 6)},
        }

    def mid(self):
        if self.up_bid is None or self.up_ask is None or self.up_bid >= self.up_ask:
            return None
        return (self.up_bid + self.up_ask) / 2

    def mid_at(self, t):
        value = None
        for at, mid in self.mids:
            if at > t:
                break
            value = mid
        return value

    def depth_qty(self, side, action, limit, t, max_age):
        """Displayed contracts executable at or better than ``limit`` (or None)."""
        if self.depth is None or t - self.depth_at > max_age:
            return None
        key = f"{side}_{'asks' if action == 'buy' else 'bids'}"
        levels = self.depth.get(key) or []
        if action == "buy":
            return sum(q for p, q in levels if p <= limit + 1e-9)
        return sum(q for p, q in levels if p >= limit - 1e-9)


def features(state: MarketState, t: float, max_quote_age: float, ai_max_age: float, use_ai: bool):
    """Return (vector, info) or (None, reason)."""
    if not state.opens <= t < state.closes:
        return None, "contract not active"
    if state.quote_age(t) > max_quote_age:
        return None, f"quotes older than {max_quote_age:.0f}s (stale prices are never traded)"
    mid = state.mid()
    if mid is None:
        return None, "no valid two-sided quote"
    if t - state.opens < 60:
        return None, "first minute of contract: building history"
    m10, m30, m60 = state.mid_at(t - 10), state.mid_at(t - 30), state.mid_at(t - 60)
    pts = [m for at, m in state.mids if at >= t - 60]
    diffs = [b - a for a, b in zip(pts, pts[1:])]
    flows = {}
    for w in (30, 60):
        signed = sum(s for at, s, _ in state.flow if at >= t - w)
        total = sum(c for at, _, c in state.flow if at >= t - w)
        flows[w] = (signed / total if total else 0.0, total)
    book_imb, book_ok = 0.0, 0.0
    if state.depth is not None and t - state.depth_at <= max_quote_age:
        b = sum(q for _, q in state.depth["up_bids"][:3])
        a = sum(q for _, q in state.depth["up_asks"][:3])
        if a + b > 0:
            book_imb, book_ok = (b - a) / (a + b), 1.0
    left = (state.closes - t) / max(1.0, state.closes - state.opens)
    x = [
        state.up_ask - state.up_bid,
        mid - 0.5,
        (mid - 0.5) * (1 - left),
        left,
        mid - m10 if m10 is not None else 0.0,
        mid - m30 if m30 is not None else 0.0,
        mid - m60 if m60 is not None else 0.0,
        math.sqrt(sum(d * d for d in diffs)),
        flows[30][0],
        flows[60][0],
        math.log1p(flows[60][1]),
        book_imb,
        book_ok,
    ]
    if use_ai:
        live_ok = state.ai_live_up is not None and t - state.ai_live_at <= ai_max_age
        x += [
            (state.ai_live_up - mid) if live_ok else 0.0,
            1.0 if live_ok else 0.0,
            (state.ai_opening_up - 0.5) if state.ai_opening_up is not None else 0.0,
            1.0 if state.ai_opening_up is not None else 0.0,
        ]
    info = {"mid": mid, "realized_vol_60s": x[7], "flow_60s": flows[60][0], "volume_60s": flows[60][1]}
    return x, info

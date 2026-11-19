"""Candidate strategy families, all executed by the baseline engine machinery.

Each family maps causal state to a predicted 30-second change of the UP mid
("pred"). The inherited engine then applies the identical cost hurdle,
spread/price/staleness filters, sizing, risk limits, latency, fills and fees.
``fair_value_hold`` additionally replaces the exit rule: it holds to official
settlement, so it pays one fee instead of two.

BTC reference (Coinbase proxy, not BRTI): a 1-second bar is usable only after
its second closes plus a 0.25 s receipt delay.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math

from ..engine import Engine, StrategyConfig
from ..features import FEATURE_NAMES_MARKET
from ..risk import taker_fee

IDX = {n: i for i, n in enumerate(FEATURE_NAMES_MARKET)}
BTC_DELAY = 0.25
FAMILIES = ("mean_reversion", "momentum_k", "flow", "vol_regime_mr", "btc_fair_gap", "fair_value_hold", "book_imbalance", "ai_gap")


@dataclass(frozen=True)
class Candidate:
    family: str
    k: float = 0.0  # scale (or theta for gap families)
    margin: float = 0.0  # fair_value_hold: required edge after fee, $/contract
    vol_regime: str = ""  # vol_regime_mr: "low" or "high"
    vol_cut: float = 0.0  # tercile cut learned on TRAIN only


def btc_events(bars):
    """[[sec, last, size, buy_size, n], ...] -> chronological btc events."""
    for sec, last, size, buy, n in bars:
        yield {"kind": "btc", "at": sec + 1 + BTC_DELAY, "sec": sec, "price": last, "size": size, "buy": buy}


def merge(*streams):
    import heapq

    order = {"market": 0, "quote": 1, "trade": 2, "btc": 2, "ai": 3, "settlement": 4}
    keyed = (((e["at"], order[e["kind"]], i, e) for e in s) for i, s in enumerate(streams))
    for *_, e in heapq.merge(*keyed, key=lambda x: (x[0], x[1], x[2])):
        yield e


class ResearchEngine(Engine):
    def __init__(self, candidate: Candidate, strategy: StrategyConfig | None = None, **kw):
        strategy = strategy or StrategyConfig()
        # Rule-based families need no regression model and no skill gate; all
        # other entry filters and costs are unchanged.
        strategy = replace(strategy, require_oos_skill=False, policy="research:" + candidate.family)
        super().__init__(strategy, name=candidate.family, **kw)
        self.cand = candidate
        self.btc = deque()  # (available_at, price)
        self.btc_ret = deque()  # (available_at, squared log return)
        self.btc_sq = 0.0
        self.anchor = {}  # ticker -> Coinbase mean over the 60 s before open
        self.fair_log = []  # (ticker, t, p_fair, mid) sampled every 30 s for calibration

    def _resolve_labels(self, t):
        """Score each candidate forecast out of sample; no regression fitting."""
        h = self.s.horizon_seconds
        while self.samples and self.samples[0][0] + h <= t:
            t0, x, mid0, pred, ticker = self.samples.popleft()
            m = self.markets.get(ticker)
            if m is None or t >= m.closes or m.quote_age(t) > self.s.max_quote_age_seconds:
                continue
            self.model.score(pred, m.mid() - mid0)

    # ---- BTC reference --------------------------------------------------
    def apply(self, e):
        if e["kind"] != "btc":
            return super().apply(e)
        if self.btc:
            r = math.log(e["price"] / self.btc[-1][1])
            self.btc_ret.append((e["at"], r * r))
            self.btc_sq += r * r
        self.btc.append((e["at"], e["price"]))
        while self.btc and self.btc[0][0] < e["at"] - 3600:
            self.btc.popleft()
        while self.btc_ret and self.btc_ret[0][0] < e["at"] - 900:
            self.btc_sq -= self.btc_ret.popleft()[1]

    def btc_price(self, t):
        return self.btc[-1][1] if self.btc and t - self.btc[-1][0] <= 5 else None

    def p_fair(self, m, t):
        """Gaussian final-average model on the Coinbase proxy (causal)."""
        s = self.btc_price(t)
        if s is None or len(self.btc_ret) < 300 or t < m.opens + 1:
            return None
        if m.ticker not in self.anchor:
            pts = [p for at, p in self.btc if m.opens - 60 + 1 + BTC_DELAY <= at <= m.opens + BTC_DELAY]
            if len(pts) < 20:
                return None
            self.anchor[m.ticker] = sum(pts) / len(pts)
        var_rate = self.btc_sq / 900.0  # per-second variance of log price
        tau = m.closes - t
        sd = s * math.sqrt(max(var_rate, 1e-12) * max(tau - 40.0, 1.0))
        return 0.5 * (1 + math.erf((s - self.anchor[m.ticker]) / (math.sqrt(2) * sd)))

    # ---- predictions ----------------------------------------------------
    def _policy_prediction(self, m, t, x, info):
        c, f = self.cand, self.cand.family
        mid = info["mid"]
        if f in ("btc_fair_gap", "fair_value_hold"):
            p = self.p_fair(m, t)
            if p is not None and int(t) % 30 == 0:
                self.fair_log.append((m.ticker, t, p, mid))
            if f == "fair_value_hold":
                self._p_now = p
                return 0.0
            return c.k * (p - mid) if p is not None else 0.0
        if f == "mean_reversion":
            return -c.k * x[IDX["ret_30s"]]
        if f == "momentum_k":
            return c.k * x[IDX["ret_30s"]]
        if f == "flow":
            return c.k * x[IDX["flow_imbalance_30s"]]
        if f == "vol_regime_mr":
            vol = x[IDX["realized_vol_60s"]]
            inside = vol < c.vol_cut if c.vol_regime == "low" else vol >= c.vol_cut
            return -c.k * x[IDX["ret_30s"]] if inside else 0.0
        if f == "book_imbalance":
            return c.k * x[IDX["book_imbalance"]] if x[IDX["book_available"]] else 0.0
        if f == "ai_gap":
            fresh = m.ai_live_up is not None and t - m.ai_live_at <= self.s.ai_max_age_seconds
            return c.k * (m.ai_live_up - mid) if fresh else 0.0
        raise ValueError(f"unknown family {f}")

    # ---- hold-to-settlement family -------------------------------------
    def _decide(self, t, m, x, info, pred, equity, why_not):
        if self.cand.family != "fair_value_hold":
            return super()._decide(t, m, x, info, pred, equity, why_not)
        if m is None or m.ticker in self.positions or any(o.ticker == m.ticker for o in self.exchange.pending):
            return super()._decide(t, m, x, info, pred, equity, why_not)
        if x is None or m.closes - t < self.s.no_entry_last_seconds or t < self.cooldown_until:
            return {"action": "WAIT", "why": "fair-value hold: no clean entry window"}
        ok, reason = self.risk.can_enter(t)
        p = getattr(self, "_p_now", None)
        if not ok or p is None:
            return {"action": "WAIT", "why": "fair-value hold: " + (reason or "no BTC fair value")}
        q = m.quotes()
        best = None
        for side, prob in (("up", p), ("down", 1 - p)):
            ask = q[side]["ask"]
            entry = ask + self.ec.limit_tolerance / 2
            edge = prob - entry - taker_fee(entry, 100) / 100  # settlement pays $1 with no exit fee
            if best is None or edge > best[0]:
                best = (edge, side, ask, q[side]["bid"])
        edge, side, ask, bid = best
        if edge < self.cand.margin or ask - bid > self.s.max_spread + 1e-9 or not self.s.min_price <= ask <= self.s.max_price:
            return {"action": "WAIT", "why": f"fair-value hold: best edge {edge*100:+.1f}c"}
        limit = min(0.99, ask + self.ec.limit_tolerance)
        displayed = m.depth_qty(side, "buy", limit, t, self.ec.max_book_age_seconds) if m.has_depth_source else None
        exposure = sum(pp.qty * pp.cost_per_contract() for pp in self.positions.values())
        qty, size_reason = self.risk.size(equity, limit, displayed, exposure, self.ec.fee_multiplier)
        if qty < 1:
            return {"action": "WAIT", "why": "fair-value hold: size zero"}
        return {"action": "BUY " + side.upper(), "side": side, "qty": qty, "quote": ask,
                "why": f"BTC fair value {p:.2f} vs ask {ask:.2f}: edge {edge*100:+.1f}c after fee; hold to settlement.",
                "entry_info": {"time_left": m.closes - t, "price": ask, "spread": ask - bid,
                               "realized_vol_60s": info["realized_vol_60s"], "volume_60s": info["volume_60s"], "hour_utc": int(t // 3600 % 24)}}

    def _exit_decision(self, t, m, pos, q, fresh, pred):
        if self.cand.family != "fair_value_hold":
            return super()._exit_decision(t, m, pos, q, fresh, pred)
        if self.risk.must_flatten() and fresh:
            return {"action": "SELL", "side": pos.side, "qty": int(pos.qty), "quote": q[pos.side]["bid"], "why": "Sell: risk limit reached."}
        return {"action": "WAIT", "why": "Holding to official settlement."}

"""Market pulse: research indicators for the dashboard (read-only, never a live input or trade signal).

Five parts, each labelled so a measured value is never shown as a validated prediction:
  1. order_book   - multi-level book imbalance per exchange -> BUYING / SELLING PRESSURE / NEUTRAL
  2. trade_flow   - aggressive buy/sell imbalance, CVD slope, large trades -> TRADE FLOW indicator
  3. momentum     - short-term momentum/acceleration/volatility -> BULLISH / BEARISH / NEUTRAL
  4. horizons     - prospective 5/15/30/60/120 s direction forecasts scored against later prices;
                    shown as NOT VALIDATED until a pre-set evidence bar is met
  5. contract     - executable Kalshi bid/ask, spread, size, fees, slippage, break-even probability and
                    EV versus the opening model; ESTIMATED EDGE stays UNVERIFIED unless the model is
                    independently validated

Inputs come only from the server snapshot (already timestamped, receipt-proven values); stale or absent
sources are reported as such, never filled in.
"""
import math
import threading
from collections import deque
from datetime import datetime, timezone

STALE_SECONDS = 5.0
HORIZONS = (5, 15, 30, 60, 120)
LEAN = 0.15                     # |imbalance| needed to call a side
MIN_BOOK_LEVELS = 5             # depth levels required before a book counts as order-book pressure
VALIDATION_MIN = 500            # scored forecasts before a horizon can be called supported
LABEL_MEASURED = "LIVE MEASURED (descriptive, not a forecast)"
LABEL_EXPERIMENTAL = "EXPERIMENTAL (research only, NOT VALIDATED)"


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _fresh(block):
    age = _num((block or {}).get("age_seconds"))
    return (block or {}).get("status") == "READY" and age is not None and age <= STALE_SECONDS


def _side(x, up="BUYING PRESSURE", down="SELLING PRESSURE"):
    if x is None:
        return "UNAVAILABLE"
    return up if x >= LEAN else down if x <= -LEAN else "NEUTRAL"


def _wilson(k, n):
    if not n:
        return None
    z, p = 1.96, k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(c - h, 4), round(c + h, 4)]


def _sources(snapshot):
    return ((snapshot.get("advanced_intelligence") or {}).get("sources") or {})


def order_book(snapshot):
    venues, readings = {}, []
    for name, src in _sources(snapshot).items():
        book = src.get("order_book") or {}
        micro = src.get("microstructure") or {}
        if not _fresh(book):
            venues[name] = {"status": "STALE OR UNAVAILABLE"}
            continue
        imb = _num(book.get("multi_level_imbalance"))
        levels = book.get("depth_levels")
        if not isinstance(levels, int) or levels < MIN_BOOK_LEVELS:
            # A 1-level "book" (e.g. Coinbase ticker best bid/ask) swings between -1 and +1 on tiny size;
            # it is shown but never used for the pressure reading.
            venues[name] = {"status": "TOP OF BOOK ONLY (not used)", "levels": levels,
                            "bid_depth": _num(book.get("bid_depth")), "ask_depth": _num(book.get("ask_depth")),
                            "age_seconds": _num(book.get("age_seconds"))}
            continue
        venues[name] = {"status": "LIVE", "imbalance": imb, "levels": levels, "top_of_book_imbalance": _num(book.get("top_of_book_imbalance")),
                        "bid_depth": _num(book.get("bid_depth")), "ask_depth": _num(book.get("ask_depth")),
                        "spread": _num(book.get("spread")), "depth_change": _num(micro.get("bid_ask_depth_change")),
                        "liquidity_deterioration": _num(micro.get("liquidity_deterioration_fraction")),
                        "age_seconds": _num(book.get("age_seconds")), "reading": _side(imb)}
        if imb is not None:
            readings.append(imb)
    sides = {_side(x) for x in readings}
    overall = _side(sum(readings) / len(readings)) if readings else "UNAVAILABLE"
    return {"label": LABEL_MEASURED, "indicator": overall, "venues": venues,
            "cross_exchange": ("CONFIRMED" if len(readings) > 1 and len(sides) == 1 and overall != "NEUTRAL"
                               else "MIXED" if len(sides) > 1 else "SINGLE SOURCE" if readings else "UNAVAILABLE")}


def trade_flow(snapshot, r10=None):
    venues, readings = {}, []
    for name, src in _sources(snapshot).items():
        flow, cvd = src.get("order_flow") or {}, src.get("cvd") or {}
        if not _fresh(flow):
            venues[name] = {"status": "STALE OR UNAVAILABLE"}
            continue
        imb = _num(flow.get("imbalance"))
        venues[name] = {"status": "LIVE", "aggressive_buy_volume": _num(flow.get("aggressive_buy_volume")),
                        "aggressive_sell_volume": _num(flow.get("aggressive_sell_volume")), "imbalance": imb,
                        "cvd_window_delta": _num(cvd.get("window_net_delta")), "cvd_slope": _num(cvd.get("slope_per_second")),
                        "cvd_acceleration": _num(cvd.get("acceleration_per_second_squared")),
                        "large_trades": flow.get("large_trade_count"), "trades_per_second": _num(flow.get("trades_per_second")),
                        "rejected_updates": src.get("rejected"), "reading": _side(imb, "AGGRESSIVE BUYING", "AGGRESSIVE SELLING")}
        if imb is not None:
            readings.append(imb)
    sides = {_side(x) for x in readings}
    out = {"label": LABEL_MEASURED,
           "indicator": _side(sum(readings) / len(readings), "AGGRESSIVE BUYING", "AGGRESSIVE SELLING") if readings else "UNAVAILABLE",
           "venues": venues,
           "cross_exchange": ("CONFIRMED" if len(readings) > 1 and len(sides) == 1 and "NEUTRAL" not in sides
                              else "MIXED" if len(sides) > 1 else "SINGLE SOURCE" if readings else "UNAVAILABLE"),
           "r10": {"status": "CORRECTED PIPELINE ISOLATED (shadow data collection); live flow uses the uncorrected path",
                   "corrected_pipeline_counters": dict(r10) if r10 else None}}
    return out


def momentum(snapshot):
    cf = (snapshot.get("advanced_intelligence") or {}).get("candidate_features") or {}
    src = _sources(snapshot)
    primary = (snapshot.get("advanced_intelligence") or {}).get("primary_source")
    flow = (src.get(primary) or {}).get("order_flow") or {}
    parts = []
    for name, weight, why in (("advanced_30s_macd_standard_histogram", 1.0, "30 s MACD histogram"),
                              ("advanced_1m_macd_standard_histogram", 1.0, "1 min MACD histogram"),
                              ("advanced_1m_ema9_distance", 1.0, "price vs 1 min EMA9"),
                              ("advanced_cvd_slope", 1.0, "CVD slope")):
        v = _num(cf.get(name))
        if v is not None and v != 0:
            parts.append((math.copysign(weight, v), why, v))
    st = _num(flow.get("short_term_momentum"))
    if st:
        parts.append((math.copysign(1.0, st), "short-term executed-price momentum", st))
    if not parts:
        return {"label": LABEL_MEASURED, "indicator": "UNAVAILABLE", "reason": "no fresh momentum inputs"}
    score = sum(p[0] for p in parts) / len(parts)
    ind = "BULLISH" if score >= 0.5 else "BEARISH" if score <= -0.5 else "NEUTRAL"
    agree = [p for p in parts if (p[0] > 0) == (score > 0)] or parts
    bw_now, bw_slow = _num(cf.get("advanced_1m_bollinger_bandwidth")), _num(cf.get("advanced_5m_bollinger_bandwidth"))
    rsi = _num(cf.get("advanced_1m_wilder_rsi14"))
    return {"label": LABEL_MEASURED, "indicator": ind, "score": round(score, 3),
            "strongest_reason": agree[0][1] if ind != "NEUTRAL" else "signals disagree",
            "volatility": ("EXPANDING" if bw_now and bw_slow and bw_now > 1.25 * bw_slow / math.sqrt(5)
                           else "CONTRACTING" if bw_now and bw_slow and bw_now < 0.75 * bw_slow / math.sqrt(5) else "STEADY")
            if bw_now and bw_slow else "UNAVAILABLE",
            "exhaustion": ("POSSIBLE (RSI " + str(round(rsi)) + ")") if rsi is not None and (rsi > 75 or rsi < 25) else "NONE",
            "inputs_used": len(parts), "note": "Describes recent movement only; not a guaranteed future move."}


class HorizonForecasts:
    """Prospective, timestamped research forecasts for short horizons, scored against later prices.

    Forecast at time t for horizon h = side of the combined pressure score (order book + trade flow +
    momentum) when it clears LEAN; target = sign(price(t+h) - price(t)) on the same source; ties excluded.
    Nothing is backfilled: a forecast is scored only by a price received at or after t+h.
    """

    def __init__(self, horizons=HORIZONS, max_pending=400):
        self.horizons = horizons
        self.pending = {h: deque(maxlen=max_pending) for h in horizons}
        self.scored = {h: [0, 0, 0] for h in horizons}       # correct, wrong, ties
        self.last = {}
        self.lock = threading.Lock()

    def observe(self, now, price, score):
        price, score = _num(price), _num(score)
        if price is None:
            return
        t = now.timestamp()
        with self.lock:
            for h in self.horizons:
                q = self.pending[h]
                while q and q[0][0] + h <= t:
                    t0, p0, side = q.popleft()
                    if price == p0:
                        self.scored[h][2] += 1
                    elif (price > p0) == (side > 0):
                        self.scored[h][0] += 1
                    else:
                        self.scored[h][1] += 1
                if score is not None and abs(score) >= LEAN:
                    q.append((t, price, 1 if score > 0 else -1))
                    self.last[h] = {"at": now.isoformat(), "direction": "UP" if score > 0 else "DOWN"}

    def report(self):
        out = {}
        with self.lock:
            for h in self.horizons:
                c, w, ties = self.scored[h]
                n = c + w
                ci = _wilson(c, n)
                supported = n >= VALIDATION_MIN and ci is not None and ci[0] > 0.5
                out[f"{h}s"] = {"status": "PROSPECTIVELY SUPPORTED (research)" if supported
                                else "NOT VALIDATED" if n else "INSUFFICIENT DATA",
                                "scored": n, "correct": c, "ties_excluded": ties,
                                "hit_rate": c / n if n else None, "hit_rate_ci95": ci,
                                "latest_forecast": self.last.get(h) if supported else None}
        return {"label": LABEL_EXPERIMENTAL, "horizons": out,
                "rule": f"direction shown only when >= {VALIDATION_MIN} prospective forecasts have a 95% lower bound above 50%; "
                        "counts reset on restart"}


def pressure_score(snapshot):
    ob, tf, mo = order_book(snapshot), trade_flow(snapshot), momentum(snapshot)
    vals = []
    for block in (ob, tf):
        rs = [v["imbalance"] for v in block["venues"].values() if v.get("imbalance") is not None]
        if rs:
            vals.append(max(-1.0, min(1.0, sum(rs) / len(rs) / 0.3)))
    if mo.get("score") is not None:
        vals.append(mo["score"])
    return sum(vals) / len(vals) if vals else None


def contract(snapshot, validated_probability=False):
    sm = snapshot.get("scalping_monitor") or {}
    sides = sm.get("sides") or {}
    opening = ((snapshot.get("prediction_engine") or {}).get("initial") or {})
    p_up = _num(opening.get("up"))
    out = {"label": "LIVE MEASURED prices; EV is EXPERIMENTAL", "ticker": sm.get("ticker"),
           "seconds_remaining": _num(sm.get("remaining_seconds")),
           "quote_age_seconds": _num((sm.get("data_ages") or {}).get("quote_seconds")), "sides": {}}
    stale = out["quote_age_seconds"] is None or out["quote_age_seconds"] > STALE_SECONDS
    best = None
    for name in ("up", "down"):
        s = sides.get(name) or {}
        ask, fee, slip = _num(s.get("ask")), _num(s.get("entry_fee")) or 0.0, _num(s.get("slippage")) or 0.0
        prob = None if p_up is None else (p_up if name == "up" else 1 - p_up)
        cost = None if ask is None else ask + fee + slip
        ev = None if cost is None or prob is None else prob * 1.0 - cost
        out["sides"][name] = {"bid": _num(s.get("bid")), "ask": ask, "spread": _num(s.get("spread")),
                              "ask_size": _num(s.get("ask_size")), "bid_size": _num(s.get("bid_size")),
                              "entry_fee": fee, "slippage": slip,
                              "break_even_probability": cost, "opening_model_probability": prob,
                              "expected_value_per_contract": ev, "max_loss_per_contract": cost}
        if ev is not None and (best is None or ev > best):
            best = ev
    if stale or best is None:
        out["estimated_edge"] = "UNVERIFIED"
        out["reason"] = "Kalshi quotes stale or the opening probability is unavailable"
    elif not validated_probability:
        out["estimated_edge"] = "UNVERIFIED"
        out["reason"] = ("The opening model has no independently validated edge; arithmetic EV is shown for research "
                         "but never called positive")
    else:
        out["estimated_edge"] = "POSITIVE" if best > 0 else "NEGATIVE"
        out["reason"] = "Validated probability versus executable ask including fees and slippage"
    out["note"] = "A predicted BTC move is not a profitable Kalshi trade; prices and fees decide that."
    return out


def build(snapshot, *, r10=None, horizons=None, validated_probability=False):
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "order_book": order_book(snapshot), "trade_flow": trade_flow(snapshot, r10), "momentum": momentum(snapshot),
            "horizons": horizons.report() if horizons else {"label": LABEL_EXPERIMENTAL, "horizons": {},
                                                            "rule": "not running"},
            "contract": contract(snapshot, validated_probability)}

"""MINUTE-6 QUANT CONSENSUS (research only, NOT VALIDATED) and Call-Gate-style market analytics.

Predicts the FINAL outcome of the same Kalshi BTC 15-minute market using only information available at the
minute-6 cutoff (Coinbase one-minute candles up to the candle that closes at opens+6:00, Kalshi's own
settlement threshold, and the live engine's P(UP) published before the cutoff). Locks once; never revised.
Stdlib only, so the alert service can run it without the server.

Components (each evaluated separately; docs/preregistration-minute6-20261122.md):
  A  price/distance: P(UP) = Phi(ln(price/threshold) / (sigma_1m * sqrt(9))), sigma from the last 60 1m returns
  B  momentum/structure: mean sign of 1m momentum, 5m momentum, EMA9-EMA21 (1m) and 5m acceleration
  C  learned: the production in-market engine's P(UP) at the cutoff (existing trained model)
Frozen rule (chosen on development markets before 2026-11-21 only): UP/DOWN only if every ELIGIBLE component
agrees (at least two eligible) with A >= 0.80 (<= 0.20), |B| >= 0.5 and C >= 0.70 (<= 0.30); otherwise WAIT.
Missing components are reported as INELIGIBLE, never invented.

Development result (docs): check 42/46 (91.3%, 33% coverage), one-time chronological holdout 31/33 (93.9%,
19% coverage), BUT Kalshi prices already near 0.90 for those calls: estimated EV after fees ~ 0, not an edge.
"""
import math
import statistics

RULE = {"a": 0.80, "b": 0.5, "c": 0.70}
HORIZON_MIN = 9
DECISION_AT_S = 360            # opens + 6:00 (the candle that closes then is the last input)
DECISION_LATEST_S = 420        # never lock later than opens + 7:00
ENGINE_CUTOFF_S = 367          # C may only use an engine P(UP) published by opens + 6:07 (strict audit boundary)
LABEL = "MINUTE-6 RESEARCH - NOT VALIDATED (predicts this market's final outcome from minute-6 information)"
# Median sigma of 1m log returns (x1e-4) by UTC hour, development markets before 2026-11-21 (historical frequency).
TYPICAL_SIGMA_1E4 = {0: 6.514, 1: 6.861, 2: 7.706, 3: 6.771, 4: 6.138, 5: 5.311, 6: 5.0, 7: 5.048, 8: 5.182,
                     9: 5.453, 10: 5.045, 11: 4.717, 12: 5.271, 13: 5.813, 14: 7.685, 15: 12.016, 16: 10.383,
                     17: 11.945, 18: 8.05, 19: 6.608, 20: 5.207, 21: 7.089, 22: 7.329, 23: 6.286}
# |15-minute BTC move| percentiles (fraction), development candles (historical frequency).
MOVE_15M = {"median": 0.00167, "p90": 0.00550, "p99": 0.01239}


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def _ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def _sign(v):
    return 0.0 if not v else math.copysign(1.0, v)


def sigma_1m(closes):
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 60, len(closes))]
    return statistics.pstdev(rets) or 1e-6


def component_a(closes, threshold, minutes_left=HORIZON_MIN):
    if len(closes) < 61 or not threshold:
        return {"status": "INELIGIBLE", "reason": "need 61 one-minute closes and the Kalshi threshold"}
    s = sigma_1m(closes)
    p = _phi(math.log(closes[-1] / threshold) / (s * math.sqrt(max(minutes_left, 1e-6))))
    d = "UP" if p >= RULE["a"] else "DOWN" if p <= 1 - RULE["a"] else "WAIT"
    return {"status": "ELIGIBLE", "p_up": p, "direction": d, "sigma_1m": s}


def component_b(closes):
    if len(closes) < 40:
        return {"status": "INELIGIBLE", "reason": "need 40 one-minute closes"}
    m1, m5 = closes[-1] / closes[-2] - 1, closes[-1] / closes[-6] - 1
    accel = m5 - (closes[-6] / closes[-11] - 1)
    ema_gap = _ema(closes[-40:], 9) - _ema(closes[-40:], 21)
    score = statistics.mean(_sign(v) for v in (m1, m5, ema_gap, accel))
    d = "UP" if score >= RULE["b"] else "DOWN" if score <= -RULE["b"] else "WAIT"
    return {"status": "ELIGIBLE", "score": score, "direction": d,
            "parts": {"momentum_1m": m1, "momentum_5m": m5, "ema9_minus_ema21": ema_gap, "acceleration_5m": accel}}


def valid_probability(value):
    return type(value) in (int, float) and 0 <= value <= 1


def component_c(engine_up):
    if not valid_probability(engine_up):
        return {"status": "INELIGIBLE", "reason": "no engine P(UP) published before the cutoff"}
    d = "UP" if engine_up >= RULE["c"] else "DOWN" if engine_up <= 1 - RULE["c"] else "WAIT"
    return {"status": "ELIGIBLE", "p_up": float(engine_up), "direction": d}


def consensus(a, b, c):
    comps = {"A_price_distance": a, "B_momentum_structure": b, "C_learned_engine": c}
    eligible = {k: v["direction"] for k, v in comps.items() if v.get("status") == "ELIGIBLE"}
    dirs = list(eligible.values())
    if len(dirs) < 2:
        direction, reason = "WAIT", "fewer than two eligible components"
    elif dirs[0] in ("UP", "DOWN") and all(x == dirs[0] for x in dirs):
        direction, reason = dirs[0], "all eligible components agree at their frozen thresholds"
    else:
        direction = "WAIT"
        reason = "components disagree or below threshold: " + ", ".join(f"{k[0]} {v}" for k, v in eligible.items())
    agree = len(dirs) >= 2 and len(set(dirs)) == 1
    score = statistics.mean([v for v in (a.get("p_up"), c.get("p_up")) if isinstance(v, (int, float))] or [0.5])
    return {"direction": direction, "reason": reason, "components_agree": agree, "eligible": len(dirs),
            "consensus_p_up_average": score, "components": comps, "rule": RULE, "label": LABEL}


def candles_to_closes(candles, opens_ts):
    """candles: iterable of (start_epoch, low, high, open, close, volume) as Coinbase returns. Closes from
    opens-65min to the candle starting at opens+5min (closes at +6:00); None if any minute is missing."""
    by = {int(c[0]): float(c[4]) for c in candles}
    out = [by.get(opens_ts + 60 * k) for k in range(-65, 6)]
    return None if None in out else out


def analytics(candles, threshold, now_ts, closes_ts, kalshi_threshold_source="Kalshi market threshold"):
    """Live measured statistics and historical frequencies (labelled). candles as in candles_to_closes."""
    cs = sorted(candles, key=lambda c: c[0])
    if len(cs) < 61 or not threshold:
        return {"status": "UNAVAILABLE", "reason": "not enough one-minute candles or no threshold"}
    closes = [float(c[4]) for c in cs]
    price, left_min = closes[-1], max((closes_ts - now_ts) / 60, 0.0)
    s = sigma_1m(closes)
    hour = int((now_ts // 3600) % 24)
    typical = TYPICAL_SIGMA_1E4[hour] / 1e4
    dist = price - threshold
    p_up = _phi(math.log(price / threshold) / (s * math.sqrt(max(left_min, 1e-6))))
    vols = [float(c[5]) for c in cs[-15:]]
    vwap = sum(float(c[4]) * float(c[5]) for c in cs[-15:]) / (sum(vols) or 1)
    quarter = [c for c in cs if int(c[0]) % 900 == 0]
    ups = downs = 0
    for q in quarter[-17:-1]:
        nxt = [c for c in cs if int(c[0]) == int(q[0]) + 840]
        if nxt:
            ups += float(nxt[0][4]) > float(q[3])
            downs += float(nxt[0][4]) <= float(q[3])
    return {
        "status": "READY", "price_source": "Coinbase BTC-USD one-minute candles (not the settlement index)",
        "threshold_source": kalshi_threshold_source,
        "measured": {"price": price, "threshold": threshold, "distance_usd": dist,
                     "side_now": "ABOVE" if dist > 0 else "BELOW",
                     "move_to_reverse_usd": -dist, "move_per_minute_usd": (-dist / left_min) if left_min else None,
                     "minutes_left": left_min, "momentum_1m": closes[-1] / closes[-2] - 1,
                     "momentum_5m": closes[-1] / closes[-6] - 1,
                     "acceleration_5m": (closes[-1] / closes[-6] - 1) - (closes[-6] / closes[-11] - 1),
                     "ema9": _ema(closes[-40:], 9), "ema21": _ema(closes[-40:], 21), "vwap_15m": vwap,
                     "volume_15m_btc": sum(vols), "sigma_1m": s},
        "historical": {"typical_sigma_1m_this_hour": typical, "volatility_vs_typical": s / typical,
                       "move_15m_fraction": MOVE_15M,
                       "coinbase_quarter_hours_last_16": {"up": ups, "down": downs,
                                                          "note": "Coinbase price quarter-hours, not Kalshi settlements"}},
        "experimental": {"reversal_probability_model": (1 - p_up) if dist > 0 else p_up,
                         "note": "random-walk estimate from current volatility; NOT a validated probability"},
    }


def latest_body(path=None, now=None):
    """Bytes for /api/minute6: the alert service's minute6_latest.json, or an honest UNAVAILABLE/STALE status."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path
    path = Path(path) if path else Path.home() / "dual-ai-alerts" / "minute6_latest.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return json.dumps({"status": "UNAVAILABLE", "label": LABEL,
                           "reason": "minute-6 research service output not found"}).encode()
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - datetime.fromisoformat(data["generated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError, OverflowError):
        age = None
    if age is None or not 0 <= age <= 90:
        data = {"status": "STALE", "label": LABEL, "reason": "minute-6 research service is not updating",
                "age_seconds": age, "scoreboard": data.get("scoreboard"), "scoreboard7": data.get("scoreboard7")}
    return json.dumps(data, default=str).encode()


def file_body(name, max_age_s=6 * 3600):
    """Bytes of a small JSON status file in ~/dual-ai-alerts (read-only), or an honest UNAVAILABLE body."""
    import json
    import time
    from pathlib import Path
    path = Path.home() / "dual-ai-alerts" / name
    try:
        data = json.loads(path.read_text())
        age = time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        return json.dumps({"status": "UNAVAILABLE", "reason": f"{name} not written yet"}).encode()
    data["file_age_s"] = round(age)
    if age > max_age_s:
        data["status"] = "STALE"
    return json.dumps(data, default=str).encode()

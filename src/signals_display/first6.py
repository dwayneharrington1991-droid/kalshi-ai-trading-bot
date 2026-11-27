"""FIRST 6 MIN shadow AI (research only, NOT VALIDATED): at the market open, P(BTC reference price at
opens+6:00 > reference price at opens+0:00). Separate target from the Kalshi final settlement; scored against
the six-minute reference move. Never changes AI #1, AI #2, COMBINED CALL or MINUTE-6 FINAL.

Inputs (closes up to the one-minute candle that ends AT the open, from the server's Coinbase feeds):
1m, 5m, 15m and 60m volatility-scaled log returns and the EMA9-EMA21 gap. Model v0: logistic regression fitted
on the earliest 60% of 640 development markets (L2 chosen on the next 20%); the last 20% scored once:
69/128 = 53.9%, Brier 0.2485 = base rate (no measurable skill; it essentially predicts the base rate and so
returns WAIT). It is installed so every opening is recorded and scored prospectively; the dashboard and phone
show "FIRST 6 MIN: TESTING 🟡" until a validated version is explicitly authorized.
"""
import math
import statistics

VERSION = "first6-logit-v0-20261122"
MU = [0.011896975209841636, -0.06237269437652663, -0.05831722690478886, -0.06795684510704834, -0.0812996503714372]
SD = [0.9331185125444774, 0.9700191049194972, 1.0400003519365806, 1.1011557836356711, 0.9545310945075148]
W = [-0.1880446901418963, -0.0025811874493000633, 0.0015794896519425708, 0.0014430791435900588, 0.0013358226834012527, -0.0020544154251313277]
THRESHOLD = 0.55


def _ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def features(closes):
    """closes: one-minute closes ending with the candle that closes at the open (at least 65 values)."""
    o = closes[-65:]
    r = [math.log(o[i] / o[i - 1]) for i in range(1, len(o))]
    s = statistics.pstdev(r[-60:]) or 1e-6
    return [math.log(o[-1] / o[-2]) / s, math.log(o[-1] / o[-6]) / (s * math.sqrt(5)),
            math.log(o[-1] / o[-16]) / (s * math.sqrt(15)), (_ema(o[-40:], 9) - _ema(o[-40:], 21)) / (o[-1] * s),
            math.log(o[-1] / o[-61]) / (s * math.sqrt(60))]


def predict(closes):
    if not closes or len(closes) < 65 or not all(valid_reference(c) for c in closes[-65:]):
        return None
    z = [1.0] + [max(-5.0, min(5.0, (f - MU[j]) / SD[j])) for j, f in enumerate(features(closes))]
    p = 1 / (1 + math.exp(-max(-30.0, min(30.0, sum(w * x for w, x in zip(W, z))))))
    return {"p_up": p, "direction": "UP" if p >= THRESHOLD else "DOWN" if p <= 1 - THRESHOLD else "WAIT",
            "version": VERSION, "reference_open": closes[-1]}


def valid_reference(price):
    return type(price) in (int, float) and math.isfinite(price) and price > 0


def _closes(candles, starts):
    result = {}
    for candle in candles:
        if not isinstance(candle, (tuple, list)) or not candle or candle[0] not in starts:
            continue
        start = candle[0]
        if len(candle) < 5 or start in result or not valid_reference(candle[4]):
            return {}
        result[start] = candle[4]
    return result


def closes_to_open(candles, opens_ts):
    """Closes from opens-65min up to the candle starting opens-1min (it ends at the open); None if incomplete."""
    starts = range(opens_ts - 65 * 60, opens_ts, 60)
    by = _closes(candles, starts)
    out = [by.get(start) for start in starts]
    return None if None in out else out


def reference_at(candles, opens_ts, minute=6):
    """Close of the candle ending at opens+minute (the six-minute reference price), or None."""
    start = opens_ts + 60 * (minute - 1)
    return _closes(candles, (start,)).get(start)

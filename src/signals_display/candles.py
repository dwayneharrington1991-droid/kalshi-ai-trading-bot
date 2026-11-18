"""Validate provider-timestamped closed bars; no partially formed/future bars."""
from datetime import datetime, timezone
import math

from . import candle_memo
from .model import number, timestamp


def _candle_context_uncached(data, now):
    try:
        if not data:
            raise ValueError("MISSING_CANDLES: one-minute BTC candles not supplied")
        received = timestamp(data["received_at"])
        if not 0 <= (now - received).total_seconds() <= 150:
            raise ValueError("STALE_CANDLES: response older than 150s or future-dated")
        bars = {}
        for bar in data["candles"]:
            start = int(bar["start"])
            if start % 60:
                raise ValueError("INCOMPATIBLE_CANDLES: one-minute start not minute-aligned")
            if start + 60 > now.timestamp():
                continue  # Exclude the still-forming candle without using its prices.
            o, h, l, c = [
                number(float(bar[k]), 0.000001, 1e12) for k in ("open", "high", "low", "close")
            ]
            v = number(float(bar["volume"]), 0, 1e15)
            if not l <= min(o, c) <= max(o, c) <= h:
                raise ValueError("INVALID_CANDLES: OHLC bounds incompatible")
            if start in bars:
                raise ValueError("INVALID_CANDLES: duplicate timestamps")
            bars[start] = (o, h, l, c, v)
        times = sorted(bars)[-61:]
        if len(times) < 31:
            raise ValueError(f"INSUFFICIENT_CANDLE_HISTORY: {len(times)}/31 closed one-minute bars")
        if now.timestamp() - (times[-1] + 60) > 150:
            raise ValueError("STALE_CANDLES: latest closed bar older than 150s")
        if any(b - a != 60 for a, b in zip(times, times[1:])):
            raise ValueError("INCOMPLETE_CANDLES: missing one-minute interval")
        closes = [bars[t][3] for t in times]
        sigma = math.sqrt(
            sum((b - a) ** 2 for a, b in zip(closes, closes[1:])) / (60 * (len(closes) - 1))
        )
        if sigma <= 0:
            raise ValueError("INSUFFICIENT_CANDLE_VARIATION: volatility is zero")
        return {
            "source": data["source"],
            "received_at": data["received_at"],
            "last_closed_at": datetime.fromtimestamp(times[-1] + 60, timezone.utc).isoformat(),
            "closed_bars": len(times),
            "momentum_1m": closes[-1] / closes[-2] - 1,
            "momentum_5m": closes[-1] / closes[-6] - 1,
            "volume_latest_btc": bars[times[-1]][4],
            "volume_5m_btc": sum(bars[t][4] for t in times[-5:]),
            "volatility_dollars_per_sqrt_second": sigma,
            "note": "Descriptive proxy momentum/volume are not evidence of profitable edge.",
        }, None
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        return {}, str(exc)


def candle_context(data, now):
    """candle_context with exact reuse while the candle content and the set of
    closed bars are unchanged (see candle_memo for the correctness contract).

    For minute-aligned bars, "start + 60 > now" excludes exactly the bars whose
    (start + 60) / 60 exceeds now // 60, so (content, now // 60) fixes every
    included bar. A success implies every bar is aligned (the original checks
    alignment before skipping forming bars). The receipt-age check (first) and
    the latest-closed-bar age check are re-evaluated on every call.
    """
    try:
        fp = candle_memo.fingerprint(data) if data else None
        received = timestamp(data["received_at"]) if fp is not None else None
        stamp = now.timestamp()
    except Exception:
        fp = None
    if fp is None or not 0 <= (now - received).total_seconds() <= 150:
        candle_memo.bypass()
        return _candle_context_uncached(data, now)
    key = ("candle_context", fp, stamp // 60)
    hit = candle_memo.get(key)
    if hit is not None:
        value, last_close = hit
        if not stamp - last_close > 150:
            return candle_memo.copy(value), None
        return _candle_context_uncached(data, now)
    value, error = _candle_context_uncached(data, now)
    if error is None:
        last_close = datetime.fromisoformat(value["last_closed_at"]).timestamp()
        candle_memo.put(key, (candle_memo.copy(value), last_close))
    return value, error

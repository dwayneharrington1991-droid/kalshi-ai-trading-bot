"""Closed-bar technical descriptors, kept separate from legacy model features.

RSI uses Wilder's recursion. EMAs use an SMA seed and are not declared ready
until two periods of closes are available. These are shadow features, not votes,
probabilities or trade recommendations. A minute candle is never split to invent
30-second history. All aggregation is UTC aligned and requires every child bar.
"""
from datetime import datetime, timezone
import math
from statistics import mean, pstdev

from .model import timestamp

TIMEFRAMES = {"30s": 30, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}
EMA_PERIODS = (9, 21, 50, 200)
VERSION = "advanced-closed-technical-v1"
FEATURE_NAMES = tuple(
    "advanced_" + label + "_" + suffix
    for label in TIMEFRAMES
    for suffix in (
        "wilder_rsi14",
        "macd_standard_histogram",
        "macd_fast_histogram",
        "bollinger_bandwidth",
        "bollinger_percent_b",
        "ema9_distance",
        "ema21_distance",
        "ema50_distance",
        "ema200_distance",
    )
)


def _time(value):
    parsed = value if isinstance(value, datetime) else timestamp(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _values(values):
    result = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("Indicator inputs must be finite positive prices")
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Indicator inputs must be finite positive prices")
        result.append(value)
    return result


def _period(period):
    if type(period) is not int or not 2 <= period <= 10000:
        raise ValueError("Indicator period must be an integer from 2 to 10000")
    return period


def seeded_ema(values, period):
    """SMA-seeded EMA series; pre-seed values are None, not invented prices."""
    values, period = _values(values), _period(period)
    output = [None] * len(values)
    if len(values) >= period:
        output[period - 1] = mean(values[:period])
        alpha = 2 / (period + 1)
        for i in range(period, len(values)):
            output[i] = output[i - 1] + alpha * (values[i] - output[i - 1])
    return output


def wilder_rsi(values, period=14):
    """Wilder RSI series, seeded with the first `period` price differences.

    A flat seed has RSI 50; gains with no losses have RSI 100. At least period+1
    closes are required, and later values use Wilder smoothing, not an SMA.
    """
    values, period = _values(values), _period(period)
    output = [None] * len(values)
    if len(values) <= period:
        return output
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = mean(max(value, 0) for value in changes[:period])
    loss = mean(max(-value, 0) for value in changes[:period])
    for i in range(period, len(values)):
        if i > period:
            change = changes[i - 1]
            gain = (gain * (period - 1) + max(change, 0)) / period
            loss = (loss * (period - 1) + max(-change, 0)) / period
        output[i] = 100 * gain / (gain + loss) if gain + loss else 50.0
    return output


def macd(values, fast=12, slow=26, signal=9):
    """SMA-seeded fast/slow EMA difference and independently seeded signal."""
    values = _values(values)
    fast, slow, signal = _period(fast), _period(slow), _period(signal)
    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")
    a, b = seeded_ema(values, fast), seeded_ema(values, slow)
    line = [None if y is None else x - y for x, y in zip(a, b)]
    # MACD can be negative: the EMA recursion itself accepts signed values here.
    available = [x for x in line if x is not None]
    signal_values = [None] * len(available)
    if len(available) >= signal:
        signal_values[signal - 1] = mean(available[:signal])
        alpha = 2 / (signal + 1)
        for i in range(signal, len(available)):
            signal_values[i] = signal_values[i - 1] + alpha * (available[i] - signal_values[i - 1])
    signal_line = [None] * min(slow - 1, len(values)) + signal_values
    histogram = [None if s is None else m - s for m, s in zip(line, signal_line)]
    return {"line": line, "signal": signal_line, "histogram": histogram}


def bollinger(values, period=20, deviations=2.0):
    """Population-standard-deviation bands; a zero-width %B is unavailable."""
    values, period = _values(values), _period(period)
    if (
        isinstance(deviations, bool)
        or not isinstance(deviations, (int, float))
        or not math.isfinite(deviations)
        or not 0 < deviations <= 10
    ):
        raise ValueError("Band deviations must be greater than zero and at most 10")
    if len(values) < period:
        return None
    center, sd = mean(values[-period:]), pstdev(values[-period:])
    lower, upper = center - deviations * sd, center + deviations * sd
    return {
        "middle": center,
        "upper": upper,
        "lower": lower,
        "bandwidth": (upper - lower) / center,
        "percent_b": (values[-1] - lower) / (upper - lower) if upper > lower else None,
        "standard_deviation": sd,
    }


def _bars(data, seconds, now):
    if not isinstance(data, dict):
        return [], "No genuine timestamped closed bars supplied", None
    try:
        receipt = _time(data["received_at"])
        if receipt > now:
            raise ValueError("Candle snapshot was received after analysis cutoff")
        rows = {}
        for raw in data.get("candles", data.get("bars", [])):
            value = raw["start"]
            if isinstance(value, bool) or float(value) != int(value):
                raise ValueError("Bar start must be an integer UTC epoch")
            start = int(value)
            if start % seconds:
                raise ValueError("Bar start is not aligned to its timeframe")
            if start + seconds > now.timestamp():
                continue
            received = _time(raw.get("first_received_at", raw.get("received_at", receipt)))
            if received > min(now, receipt) or received.timestamp() < start + seconds:
                continue
            if any(isinstance(raw[key], bool) for key in ("open", "high", "low", "close")):
                raise ValueError("Boolean OHLC values are unsupported")
            bar = {key: float(raw[key]) for key in ("open", "high", "low", "close")}
            if any(not math.isfinite(v) or v <= 0 for v in bar.values()):
                raise ValueError("Non-finite or non-positive OHLC value")
            if (
                not bar["low"]
                <= min(bar["open"], bar["close"])
                <= max(bar["open"], bar["close"])
                <= bar["high"]
            ):
                raise ValueError("Incompatible OHLC bounds")
            volume = raw.get("volume")
            if volume is not None:
                if isinstance(volume, bool):
                    raise ValueError("Boolean volume is unsupported")
                volume = float(volume)
                if not math.isfinite(volume) or volume < 0:
                    raise ValueError("Invalid candle volume")
            if start in rows:
                raise ValueError("Duplicate or contradictory timestamped bars")
            rows[start] = dict(bar, start=start, volume=volume, received_at=received.isoformat())
        keys = sorted(rows)
        if not keys:
            return [], "No closed bars received by analysis cutoff", receipt.isoformat()
        # A gap resets indicator continuity. Never carry recursive state across it.
        index = len(keys) - 1
        while index > 0 and keys[index] - keys[index - 1] == seconds:
            index -= 1
        suffix = keys[index:]
        age = now.timestamp() - (suffix[-1] + seconds)
        if age > max(120, seconds * 2):
            return [], "Latest closed bar is stale", receipt.isoformat()
        reason = "Continuity restarted after a missing bar" if index else "Contiguous closed bars"
        return [rows[k] for k in suffix], reason, receipt.isoformat()
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError) as exc:
        return [], str(exc), None


def _aggregate(bars, seconds):
    if seconds == 60:
        return bars
    grouped = {}
    for bar in bars:
        grouped.setdefault(bar["start"] // seconds * seconds, []).append(bar)
    output = []
    for start, group in sorted(grouped.items()):
        if len(group) != seconds // 60 or [b["start"] for b in group] != list(
            range(start, start + seconds, 60)
        ):
            continue
        output.append(
            {
                "start": start,
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
                "volume": sum(b["volume"] for b in group)
                if all(b["volume"] is not None for b in group)
                else None,
                "received_at": max(b["received_at"] for b in group),
            }
        )
    # Missing aggregation buckets must also reset continuity.
    index = len(output) - 1
    while index > 0 and output[index]["start"] - output[index - 1]["start"] == seconds:
        index -= 1
    return output[max(index, 0) :]


def _divergence(prices, oscillator):
    """Compare two confirmed 2-left/2-right pivots; no unconfirmed endpoints."""
    highs, lows = [], []
    start = max(2, len(prices) - 80)
    for i in range(start, len(prices) - 2):
        if oscillator[i] is None:
            continue
        around = prices[i - 2 : i] + prices[i + 1 : i + 3]
        if all(prices[i] > x for x in around):
            highs.append(i)
        if all(prices[i] < x for x in around):
            lows.append(i)
    if len(highs) >= 2:
        a, b = highs[-2:]
        if b - a >= 3 and prices[b] > prices[a] and oscillator[b] < oscillator[a]:
            return "BEARISH_DIVERGENCE"
    if len(lows) >= 2:
        a, b = lows[-2:]
        if b - a >= 3 and prices[b] < prices[a] and oscillator[b] > oscillator[a]:
            return "BULLISH_DIVERGENCE"
    return "NONE_CONFIRMED"


def _cross(a0, b0, a1, b1):
    if None in (a0, b0, a1, b1):
        return "NONE"
    return "BULLISH" if a0 <= b0 and a1 > b1 else "BEARISH" if a0 >= b0 and a1 < b1 else "NONE"


def _unavailable(reason, required, actual):
    return {
        "status": "UNAVAILABLE",
        "reason": reason,
        "required_closed_bars": required,
        "closed_bars": actual,
        "interpretation": "UNAVAILABLE",
        "actively_contributing": False,
    }


def _frame(bars, seconds, now, reason, deviations):
    closes, count = [bar["close"] for bar in bars], len(bars)
    last = bars[-1] if bars else None
    result = {
        "closed_bars": count,
        "last_closed_at": datetime.fromtimestamp(last["start"] + seconds, timezone.utc).isoformat()
        if last
        else None,
        "data_age_seconds": now.timestamp() - last["start"] - seconds if last else None,
        "continuity": reason,
        "source": None,
        "max_input_received_at": max((b["received_at"] for b in bars), default=None),
    }
    rsi = wilder_rsi(closes)
    if count >= 15:
        value = rsi[-1]
        previous = rsi[-2] if count > 15 else None
        result["rsi"] = {
            "status": "READY",
            "value": value,
            "period": 14,
            "smoothing": "Wilder",
            "momentum_change": value - previous if previous is not None else None,
            "condition": "OVERBOUGHT"
            if value >= 70
            else "OVERSOLD"
            if value <= 30
            else "MID_RANGE",
            "interpretation": "BULLISH" if value > 50 else "BEARISH" if value < 50 else "NEUTRAL",
            "divergence": _divergence(closes, rsi),
            "potential_reversal": "UP_FROM_OVERSOLD"
            if previous is not None and previous <= 30 < value
            else "DOWN_FROM_OVERBOUGHT"
            if previous is not None and previous >= 70 > value
            else "NONE",
            "actively_contributing": False,
        }
    else:
        result["rsi"] = _unavailable("Wilder RSI14 requires 15 closed bars", 15, count)
    result["macd"] = {}
    for label, fast, slow, signal in (("standard", 12, 26, 9), ("fast", 6, 13, 5)):
        required = max(2 * slow, slow + 2 * signal - 1)
        if count < required:
            result["macd"][label] = _unavailable(
                "Seeded MACD requires additional EMA/signal warmup", required, count
            )
            continue
        values = macd(closes, fast, slow, signal)
        line, sig, hist = values["line"], values["signal"], values["histogram"]
        result["macd"][label] = {
            "status": "READY",
            "parameters": [fast, slow, signal],
            "line": line[-1],
            "signal": sig[-1],
            "histogram": hist[-1],
            "histogram_momentum": hist[-1] - hist[-2],
            "crossover": _cross(line[-2], sig[-2], line[-1], sig[-1]),
            "divergence": _divergence(closes, line),
            "interpretation": "BULLISH"
            if hist[-1] > 0
            else "BEARISH"
            if hist[-1] < 0
            else "NEUTRAL",
            "actively_contributing": False,
        }
    bands = bollinger(closes, deviations=deviations)
    if bands is None:
        result["bollinger"] = _unavailable("Bollinger20 requires 20 closed bars", 20, count)
    else:
        prior = bollinger(closes[:-1], deviations=deviations)
        change = bands["bandwidth"] - prior["bandwidth"] if prior else None
        result["bollinger"] = dict(
            bands,
            status="READY",
            period=20,
            deviations=deviations,
            bandwidth_change=change,
            regime="EXPANDING"
            if change is not None and change > 0
            else "CONTRACTING"
            if change is not None and change < 0
            else "UNCHANGED",
            breakout="ABOVE"
            if closes[-1] > bands["upper"]
            else "BELOW"
            if closes[-1] < bands["lower"]
            else "INSIDE",
            interpretation="BULLISH"
            if closes[-1] > bands["middle"]
            else "BEARISH"
            if closes[-1] < bands["middle"]
            else "NEUTRAL",
            actively_contributing=False,
        )
    averages, series = {}, {}
    for period in EMA_PERIODS:
        required = 2 * period
        if count < required:
            averages[str(period)] = _unavailable(
                "SMA seed plus one full period of EMA warmup required", required, count
            )
        else:
            values = seeded_ema(closes, period)
            series[period] = values
            slope = values[-1] - values[-2]
            averages[str(period)] = {
                "status": "READY",
                "value": values[-1],
                "price_distance_fraction": closes[-1] / values[-1] - 1,
                "slope_per_bar": slope,
                "acceleration_per_bar_squared": slope - (values[-2] - values[-3]),
                "interpretation": "BULLISH"
                if closes[-1] > values[-1]
                else "BEARISH"
                if closes[-1] < values[-1]
                else "NEUTRAL",
                "actively_contributing": False,
            }
    known = [
        averages[str(p)]["value"] for p in EMA_PERIODS if averages[str(p)]["status"] == "READY"
    ]
    alignment = (
        "UNAVAILABLE"
        if len(known) < 2
        else "NEUTRAL"
        if max(known) == min(known)
        else "BULLISH"
        if all(a > b for a, b in zip(known, known[1:]))
        else "BEARISH"
        if all(a < b for a, b in zip(known, known[1:]))
        else "MIXED"
    )
    crossovers = {}
    for fast, slow in ((9, 21), (21, 50), (50, 200)):
        crossovers[f"{fast}_{slow}"] = (
            _cross(series[fast][-2], series[slow][-2], series[fast][-1], series[slow][-1])
            if fast in series and slow in series
            else "UNAVAILABLE"
        )
    result["ema"] = {
        "status": "READY" if known else "UNAVAILABLE",
        "periods": averages,
        "alignment": alignment,
        "alignment_periods": [
            p for p in EMA_PERIODS if str(p) in averages and averages[str(p)]["status"] == "READY"
        ],
        "all_periods_ready": len(known) == 4,
        "crossovers": crossovers,
        "trend_strength_fraction": abs(known[0] - known[-1]) / closes[-1]
        if len(known) >= 2
        else None,
        "interpretation": alignment,
        "actively_contributing": False,
    }
    return result


def technical_snapshot(candles, now, *, short_bars=None, deviations=2.0):
    """Return receipt-safe multi-timeframe readings and shadow feature values.

    `candles` is the existing 1m {received_at, source, candles:[OHLCV]} format.
    `short_bars` uses the same shape for actual complete 30s OHLCV only. Receipts
    after `now`, forming bars, unaligned bars and invalid OHLC are not used.
    Input gaps reset recursion. Advanced values never alter probabilities here.
    """
    now = _time(now)
    # Validate configuration even while no data is available.
    bollinger([], deviations=deviations)
    minutes, reason, receipt = _bars(candles, 60, now)
    shorts, short_reason, short_receipt = _bars(short_bars, 30, now)
    frames, candidates, feature_sources = {}, {}, {}
    for label, seconds in TIMEFRAMES.items():
        bars = shorts if seconds == 30 else _aggregate(minutes, seconds)
        frame = _frame(bars, seconds, now, short_reason if seconds == 30 else reason, deviations)
        source_data = short_bars if seconds == 30 else candles
        frame["source"] = source_data.get("source") if isinstance(source_data, dict) else None
        frame["received_at"] = short_receipt if seconds == 30 else receipt
        frames[label] = frame
        prefix = "advanced_" + label + "_"
        candidates[prefix + "wilder_rsi14"] = frame["rsi"].get("value")
        for variant in ("standard", "fast"):
            candidates[prefix + "macd_" + variant + "_histogram"] = frame["macd"][variant].get(
                "histogram"
            )
        candidates[prefix + "bollinger_bandwidth"] = frame["bollinger"].get("bandwidth")
        candidates[prefix + "bollinger_percent_b"] = frame["bollinger"].get("percent_b")
        for period in EMA_PERIODS:
            candidates[prefix + f"ema{period}_distance"] = frame["ema"]["periods"][str(period)].get(
                "price_distance_fraction"
            )
        for name in FEATURE_NAMES:
            if name.startswith(prefix) and candidates.get(name) is not None:
                feature_sources[name] = {
                    "received_at": frame["max_input_received_at"],
                    "event_at": frame["last_closed_at"],
                }
    families = {}
    for family in ("rsi", "macd", "bollinger", "ema"):
        base = frames["1m"][family]
        if family == "macd":
            base = base["standard"]
        for frame in frames.values():
            indicator = frame[family]
            if family == "macd":
                for variant in indicator.values():
                    variant.update(
                        as_of=frame["last_closed_at"],
                        age_seconds=frame["data_age_seconds"],
                        contributing=False,
                    )
            else:
                indicator.update(
                    as_of=frame["last_closed_at"],
                    age_seconds=frame["data_age_seconds"],
                    contributing=False,
                )
        families[family] = {
            "status": base["status"],
            "reading": {label: frame[family] for label, frame in frames.items()},
            "interpretation": base.get("interpretation", "UNAVAILABLE"),
            "as_of": frames["1m"]["last_closed_at"],
            "age_seconds": frames["1m"]["data_age_seconds"],
            "contributing": False,
            "reason": "Shadow descriptors await chronological incremental-value validation",
        }
    ready = any(frame["rsi"]["status"] == "READY" for frame in frames.values())
    return {
        "version": VERSION,
        "status": "READY" if ready else "WARMING_UP",
        "as_of": now.isoformat(),
        "timeframes": frames,
        "families": families,
        "candidate_features": candidates,
        "candidate_feature_sources": feature_sources,
        "max_input_received_at": max(
            (
                frame["max_input_received_at"]
                for frame in frames.values()
                if frame["max_input_received_at"]
            ),
            default=None,
        ),
        "actively_contributing": False,
        "probability_adjustment": 0.0,
        "interpretation_note": "Descriptive shadow features; bullish/bearish readings are not validated settlement probabilities or trading edges.",
        "warmup_policy": "EMA uses an SMA seed plus one additional full period; MACD applies analogous EMA/signal warmup. No synthetic 30-second bars.",
    }

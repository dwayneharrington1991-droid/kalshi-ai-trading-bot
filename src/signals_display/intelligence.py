"""Bounded, receipt-time market intelligence; observations are never trade calls.

Half-second bins cover the latest five minutes and minute bins retain 24 hours.
Price diagnostics use only observations received by the requested UTC cutoff.
Trade volume and direction are never inferred from quote updates or candles.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
from statistics import mean, pstdev
import threading

from .model import number, timestamp


HORIZONS = (0.5, 1, 5, 15, 30, 60, 180, 300, 900, 1800, 3600, 14400, 86400)
WINDOW_LABELS = (
    "500ms",
    "1s",
    "5s",
    "15s",
    "30s",
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "24h",
)


def _time(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = timestamp(value)
    else:
        raise ValueError("Timestamp must be an aware datetime or ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class _Bin:
    bucket: int
    generation: int
    first_at: float
    last_at: float
    first_price: float
    last_price: float
    high: float
    low: float
    samples: int = 0
    squared_log_returns: float = 0.0
    signed_changes: int = 0
    nonzero_changes: int = 0
    maximum_gap: float = 0.0
    trades: int = 0
    known_volume_trades: int = 0
    known_direction_trades: int = 0
    volume: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    first_spread: float | None = None
    spread: float | None = None
    spread_at: float | None = None
    first_depth: float | None = None
    depth: float | None = None
    depth_at: float | None = None
    imbalance: float | None = None
    latest_event_at: float | None = None

    def add(self, item):
        when, price = item["received"], item["price"]
        if self.samples:
            delta = price - self.last_price
            self.squared_log_returns += math.log(price / self.last_price) ** 2
            self.signed_changes += 1 if delta > 0 else -1 if delta < 0 else 0
            self.nonzero_changes += int(delta != 0)
            self.maximum_gap = max(self.maximum_gap, when - self.last_at)
        self.samples += 1
        self.last_at, self.last_price = when, price
        self.high, self.low = max(self.high, price), min(self.low, price)
        if item["event"] is not None:
            self.latest_event_at = max(self.latest_event_at or item["event"], item["event"])
        if item["kind"] == "trade":
            self.trades += 1
            if item["volume"] is not None:
                self.known_volume_trades += 1
                self.volume += item["volume"]
                if item["aggressor"] is not None:
                    self.known_direction_trades += 1
                    if item["aggressor"] == "buy":
                        self.buy_volume += item["volume"]
                    else:
                        self.sell_volume += item["volume"]
        if item["spread"] is not None:
            if self.first_spread is None:
                self.first_spread = item["spread"]
            self.spread = item["spread"]
            self.spread_at = when
        if item["depth"] is not None:
            if self.first_depth is None:
                self.first_depth = item["depth"]
            self.depth = item["depth"]
            self.depth_at = when
            self.imbalance = item["imbalance"]


class _Source:
    def __init__(self):
        self.fine = deque(maxlen=602)
        self.minutes = deque(maxlen=1442)
        self.seen = set()
        self.ids = deque(maxlen=4096)
        self.generation = 0
        self.session = None
        self.last_received = None
        self.last_event = None
        self.event_lag = None
        self.processing_ms = None
        self.reset_count = 0
        self.dropped_updates = 0
        self.rejected = 0
        self.latest = None

    def reset(self, session):
        self.session = session
        self.generation += 1
        if self.last_received is not None:
            self.reset_count += 1
        self.last_event = None
        self.ids.clear()
        self.seen.clear()


class MarketIntelligence:
    """Per-source analytics, not independent votes or confidence adjustments.

    ``observe(mapping)`` consumes streaming observation dictionaries. ``ingest``
    accepts the same fields as keyword arguments. A new session or explicit gap
    restarts continuity; retained old bins cannot silently fill the missing path.
    """

    def __init__(self, *, max_sources=8):
        if type(max_sources) is not int or not 1 <= max_sources <= 32:
            raise ValueError("Source count must be 1..32")
        self.max_sources = max_sources
        self.sources = {}
        self.lock = threading.RLock()

    def observe(self, observation):
        if not isinstance(observation, dict):
            raise ValueError("Observation must be a mapping")
        return self.ingest(**observation)

    def ingest(
        self,
        source,
        price,
        event_at=None,
        received_at=None,
        *,
        bid=None,
        ask=None,
        bid_depth=None,
        ask_depth=None,
        volume=None,
        aggressor=None,
        trade_id=None,
        session_id=None,
        session=None,
        kind="quote",
        gap_detected=False,
        missing_updates_count=0,
        received_mono=None,
        processed_mono=None,
        **_metadata,
    ):
        if not isinstance(source, str) or not source or len(source) > 128:
            raise ValueError("A bounded source name is required")
        at = _time(received_at)
        event = _time(event_at) if event_at is not None else None
        price = number(price, 0.000001, 1e12)
        if event and not 0 <= (at - event).total_seconds() <= 5:
            raise ValueError("Event timestamp is future-dated or stale at receipt")
        if kind not in ("quote", "trade"):
            raise ValueError("Observation kind must be quote or trade")
        if aggressor not in (None, "buy", "sell"):
            raise ValueError("Unknown aggressor side")
        if (bid is None) != (ask is None):
            raise ValueError("Bid and ask must be supplied together")
        spread = None
        if bid is not None:
            bid, ask = number(bid, 0.000001, 1e12), number(ask, 0.000001, 1e12)
            if bid > ask:
                raise ValueError("Crossed book")
            spread = ask - bid
        if (bid_depth is None) != (ask_depth is None):
            raise ValueError("Both depth quantities must be supplied together")
        depth = imbalance = None
        if bid_depth is not None:
            buy, sell = number(bid_depth, 0, 1e15), number(ask_depth, 0, 1e15)
            depth = buy + sell
            imbalance = (buy - sell) / depth if depth else None
        if volume is not None:
            volume = number(volume, 0, 1e15)
        if type(missing_updates_count) is not int or missing_updates_count < 0:
            raise ValueError("Missing update count must be a nonnegative integer")
        processing = None
        if (received_mono is None) != (processed_mono is None):
            raise ValueError("Both monotonic timestamps are required")
        if received_mono is not None:
            received_mono = number(received_mono, 0, 1e15)
            processed_mono = number(processed_mono, received_mono, 1e15)
            processing = (processed_mono - received_mono) * 1000
        identity = session_id if session_id is not None else session
        if identity is not None and (
            not isinstance(identity, (str, int)) or len(str(identity)) > 128
        ):
            raise ValueError("Session identifier is invalid")
        if trade_id is not None and (
            not isinstance(trade_id, (str, int)) or len(str(trade_id)) > 160
        ):
            raise ValueError("Trade identifier is invalid")
        receipt = at.timestamp()
        item = dict(
            received=receipt,
            event=event.timestamp() if event else None,
            price=price,
            spread=spread,
            depth=depth,
            imbalance=imbalance,
            kind=kind,
            volume=volume,
            aggressor=aggressor,
        )
        with self.lock:
            if source not in self.sources:
                if len(self.sources) >= self.max_sources:
                    raise ValueError("Maximum source count reached")
                self.sources[source] = _Source()
            state = self.sources[source]
            if state.last_received is not None and receipt < state.last_received:
                state.rejected += 1
                raise ValueError("Out-of-order receipt timestamp")
            if identity != state.session or gap_detected:
                state.reset(identity)
                state.dropped_updates += missing_updates_count
            key = (identity, str(trade_id)) if kind == "trade" and trade_id is not None else None
            if key is not None and key in state.seen:
                return False
            if event and state.last_event is not None and event.timestamp() < state.last_event:
                state.rejected += 1
                raise ValueError("Out-of-order exchange timestamp")
            if kind == "trade" and trade_id is None:
                # A price observation is valid, but repeated anonymous trade
                # messages must not invent transaction counts/volume.
                item["kind"] = "quote"
            if key is not None:
                if len(state.ids) == state.ids.maxlen:
                    state.seen.discard(state.ids[0])
                state.ids.append(key)
                state.seen.add(key)
            for collection, resolution, retention in (
                (state.fine, 0.5, 300),
                (state.minutes, 60, 86400),
            ):
                bucket = math.floor(receipt / resolution)
                while collection and collection[0].last_at < receipt - retention:
                    collection.popleft()
                if (
                    not collection
                    or collection[-1].bucket != bucket
                    or collection[-1].generation != state.generation
                ):
                    collection.append(
                        _Bin(bucket, state.generation, receipt, receipt, price, price, price, price)
                    )
                collection[-1].add(item)
            state.last_received = receipt
            state.last_event = event.timestamp() if event else state.last_event
            state.event_lag = (at - event).total_seconds() if event else None
            state.processing_ms = processing
            state.latest = {
                "received_at": at.isoformat(),
                "event_at": event.isoformat() if event else None,
                "price": price,
            }
            return True

    def reset(self, source, *, session_id=None):
        """Invalidate continuity after reconnect without deleting retained bins."""
        with self.lock:
            if source in self.sources:
                self.sources[source].reset(session_id)

    @staticmethod
    def _window(bins, now, seconds, resolution):
        cutoff = now.timestamp()
        start = cutoff - seconds
        eligible = [
            b
            for b in bins
            if b.last_at <= cutoff and (b.latest_event_at is None or b.latest_event_at <= cutoff)
        ]
        if not eligible:
            return {
                "status": "UNAVAILABLE",
                "reason": "No observations received by cutoff",
                "features": {},
            }
        generation = eligible[-1].generation
        rows = [b for b in eligible if b.generation == generation and b.first_at >= start]
        base = {
            "status": "WARMING UP",
            "reason": "Window coverage or update continuity is insufficient",
            "requested_seconds": seconds,
            "bin_resolution_seconds": resolution,
            "features": {},
        }
        if not rows:
            return base
        samples = sum(b.samples for b in rows)
        span = rows[-1].last_at - rows[0].first_at
        maximum_gap = max(
            [b.maximum_gap for b in rows]
            + [b.first_at - a.last_at for a, b in zip(rows, rows[1:])]
            + [0]
        )
        edge_tolerance = min(5, max(resolution, seconds * 0.2))
        continuous = (
            samples >= 2
            and span >= seconds * 0.8
            and rows[0].first_at - start <= max(resolution, edge_tolerance)
            and cutoff - rows[-1].last_at <= min(5, max(0.1, seconds * 0.2))
            and maximum_gap <= min(5, max(0.5, seconds))
        )
        base.update(
            observed_samples=samples,
            effective_seconds=span,
            maximum_observation_gap_seconds=maximum_gap,
            mean_observation_interval_seconds=span / (samples - 1) if samples > 1 else None,
            first_received_at=datetime.fromtimestamp(rows[0].first_at, timezone.utc).isoformat(),
            last_received_at=datetime.fromtimestamp(rows[-1].last_at, timezone.utc).isoformat(),
            data_age_seconds=cutoff - rows[-1].last_at,
        )
        if not continuous:
            return base
        first, last = rows[0].first_price, rows[-1].last_price
        squared = sum(b.squared_log_returns for b in rows) + sum(
            math.log(b.first_price / a.last_price) ** 2 for a, b in zip(rows, rows[1:])
        )
        changes = sum(b.signed_changes for b in rows)
        nonzero = sum(b.nonzero_changes for b in rows)
        for a, b in zip(rows, rows[1:]):
            delta = b.first_price - a.last_price
            changes += 1 if delta > 0 else -1 if delta < 0 else 0
            nonzero += int(delta != 0)
        midpoint = rows[0].first_at + span / 2
        halves = [
            [b for b in rows if b.last_at <= midpoint],
            [b for b in rows if b.first_at >= midpoint],
        ]
        velocities = [
            (group[-1].last_price - group[0].first_price) / (group[-1].last_at - group[0].first_at)
            if group and group[-1].last_at > group[0].first_at
            else None
            for group in halves
        ]
        trades = sum(b.trades for b in rows)
        known_volume = sum(b.known_volume_trades for b in rows)
        known_direction = sum(b.known_direction_trades for b in rows)
        volume = sum(b.volume for b in rows) if trades and known_volume == trades else None
        buy, sell = sum(b.buy_volume for b in rows), sum(b.sell_volume for b in rows)
        spreads = [b for b in rows if b.spread is not None]
        depths = [b for b in rows if b.depth is not None]
        spread_fresh = bool(spreads and cutoff - spreads[-1].spread_at <= 5)
        depth_fresh = bool(depths and cutoff - depths[-1].depth_at <= 5)
        features = {
            "return_fraction": last / first - 1,
            "velocity_dollars_per_second": (last - first) / span,
            "acceleration_dollars_per_second_squared": (velocities[1] - velocities[0]) / (span / 2)
            if all(v is not None for v in velocities)
            else None,
            "directional_persistence": changes / nonzero if nonzero else 0.0,
            "realized_volatility_per_sqrt_second": math.sqrt(squared / span),
            "observed_high": max(b.high for b in rows),
            "observed_low": min(b.low for b in rows),
            "trade_frequency_per_second": trades / span if trades else None,
            "trade_volume": volume,
            "aggressive_buy_volume": buy
            if volume is not None and known_direction == trades
            else None,
            "aggressive_sell_volume": sell
            if volume is not None and known_direction == trades
            else None,
            "trade_flow_imbalance": (buy - sell) / (buy + sell)
            if volume is not None and known_direction == trades and buy + sell
            else None,
            "spread": spreads[-1].spread if spread_fresh else None,
            "spread_change": spreads[-1].spread - spreads[0].first_spread if spread_fresh else None,
            "depth_imbalance": depths[-1].imbalance if depth_fresh else None,
            "depth_change": depths[-1].depth - depths[0].first_depth if depth_fresh else None,
            "liquidity_withdrawal_fraction": (1 - depths[-1].depth / depths[0].first_depth)
            if depth_fresh and depths[0].first_depth > 0
            else None,
            "price_impact_per_net_volume": (last - first) / (buy - sell)
            if volume is not None and known_direction == trades and abs(buy - sell) > 1e-12
            else None,
        }
        base.update(
            status="READY",
            reason="Observed source-specific window; descriptive candidates, not calibrated forecasts",
            features=features,
        )
        return base

    def summarize(self, now, *, candles=None, primary_source=None):
        now = _time(now)
        result = {
            "status": "EXPERIMENTAL",
            "cutoff": now.isoformat(),
            "sources": {},
            "candidate_features": {},
            "feature_availability": {},
            "probability_adjustment": 0.0,
            "validation": "Descriptive, correlated candidate features only; no predictive improvement or profitable edge demonstrated",
            "timing_note": "Event-to-receipt difference includes exchange delay, network and clock error; it is not a measured network round trip. Unknown event timestamps remain unavailable.",
        }
        with self.lock:
            for name, state in self.sources.items():
                windows = {
                    label: self._window(
                        state.fine if seconds <= 300 else state.minutes,
                        now,
                        seconds,
                        0.5 if seconds <= 300 else 60,
                    )
                    for seconds, label in zip(HORIZONS, WINDOW_LABELS)
                }
                eligible = [
                    b
                    for b in state.fine
                    if b.last_at <= now.timestamp()
                    and (b.latest_event_at is None or b.latest_event_at <= now.timestamp())
                ]
                latest = eligible[-1] if eligible else None
                age = now.timestamp() - latest.last_at if latest else None
                result["sources"][name] = {
                    "status": "LIVE" if age is not None and 0 <= age <= 5 else "STALE",
                    "windows": windows,
                    "data_age_seconds": age,
                    "latest_price": latest.last_price if latest else None,
                    "event_to_receipt_seconds": state.event_lag
                    if latest and latest.last_at == state.last_received
                    else None,
                    "processing_latency_ms": state.processing_ms
                    if latest and latest.last_at == state.last_received
                    else None,
                    "network_latency_ms": None,
                    "reconnection_or_gap_resets": state.reset_count,
                    "reported_dropped_updates": state.dropped_updates,
                    "rejected_updates": state.rejected,
                    "retained_fine_bins": len(state.fine),
                    "retained_minute_bins": len(state.minutes),
                    "retention_note": "At most 602 half-second bins and 1442 minute bins per source; reconnects invalidate continuity, not raw audit journals",
                }
        if primary_source is not None and primary_source in result["sources"]:
            windows = result["sources"][primary_source]["windows"]
            for label in WINDOW_LABELS:
                for feature in (
                    "return_fraction",
                    "velocity_dollars_per_second",
                    "acceleration_dollars_per_second_squared",
                    "realized_volatility_per_sqrt_second",
                    "trade_flow_imbalance",
                    "depth_imbalance",
                    "liquidity_withdrawal_fraction",
                ):
                    name = "micro_" + label + "_" + feature
                    value = (
                        windows[label]["features"].get(feature)
                        if windows[label]["status"] == "READY"
                        else None
                    )
                    result["candidate_features"][name] = value
                    result["feature_availability"][name] = (
                        "AVAILABLE_EXPERIMENTAL" if value is not None else windows[label]["reason"]
                    )
        if candles is not None:
            result["closed_candle_context"] = technical_context(candles, now)
        return result


def technical_context(data, now):
    """Closed, received-by-cutoff candles; never consume a developing candle."""
    now = _time(now)
    result = {
        "status": "UNAVAILABLE",
        "features": {},
        "reason": "Need contiguous closed historical candles",
    }
    try:
        receipt = _time(data["received_at"])
        if not 0 <= (now - receipt).total_seconds() <= 150:
            raise ValueError("Candle receipt is stale or after cutoff")
        rows = {}
        for raw in data.get("candles", []):
            start = int(raw["start"])
            if (
                start + 60 > min(now.timestamp(), receipt.timestamp())
                or start < now.timestamp() - 86460
            ):
                continue
            if start % 60 or start in rows:
                raise ValueError("Unaligned or duplicate candle")
            bar = {
                key: number(float(raw[key]), 0 if key == "volume" else 0.000001, 1e15)
                for key in ("open", "high", "low", "close", "volume")
            }
            if (
                not bar["low"]
                <= min(bar["open"], bar["close"])
                <= max(bar["open"], bar["close"])
                <= bar["high"]
            ):
                raise ValueError("Invalid candle bounds")
            rows[start] = bar
        keys = sorted(rows)
        if len(keys) < 60 or any(b - a != 60 for a, b in zip(keys, keys[1:])):
            raise ValueError("Need at least 60 contiguous closed minutes")
        if now.timestamp() - keys[-1] - 60 > 120:
            raise ValueError("Latest closed candle is stale")
        bars = [rows[key] for key in keys]
        closes = [b["close"] for b in bars]
        returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        true_ranges = [
            max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"]))
            for a, b in zip(bars, bars[1:])
        ]
        from .prediction_engine import ema

        e12, e26, e50 = ema(closes, 12), ema(closes, 26), ema(closes, 50)
        macd = [a - b for a, b in zip(e12, e26)]
        movements = [b - a for a, b in zip(closes, closes[1:])]
        gains = mean(max(x, 0) for x in movements[-14:])
        losses = mean(max(-x, 0) for x in movements[-14:])
        bmean, bdev = mean(closes[-20:]), pstdev(closes[-20:])
        squared = [x * x for x in returns[-60:]]
        left, right = squared[:-1], squared[1:]
        lm, rm = mean(left), mean(right)
        denominator = math.sqrt(
            sum((x - lm) ** 2 for x in left) * sum((x - rm) ** 2 for x in right)
        )
        cluster = (
            sum((a - lm) * (b - rm) for a, b in zip(left, right)) / denominator
            if denominator
            else None
        )
        recent_vol = sum(x * x for x in returns[-5:]) / 5
        older_vol = sum(x * x for x in returns[-60:-5]) / len(returns[-60:-5])
        recent_volume = sum(b["volume"] for b in bars[-5:]) / 5
        old_volume = mean(b["volume"] for b in bars[-60:-5])
        features = {
            "atr_14": mean(true_ranges[-14:]),
            "rsi_14": 100 * gains / (gains + losses) if gains + losses else 50.0,
            "macd": macd[-1],
            "macd_histogram": macd[-1] - ema(macd, 9)[-1],
            "ema_50_slope_per_minute": (e50[-1] - e50[-6]) / 5,
            "bollinger_z": (closes[-1] - bmean) / bdev if bdev else None,
            "bollinger_width_fraction": 4 * bdev / bmean,
            "volatility_clustering_lag1": cluster,
            "volatility_expansion_5m_vs_prior55m": recent_vol / older_vol if older_vol else None,
            "relative_volume_5m_vs_prior55m": recent_volume / old_volume if old_volume else None,
            "support_20m": min(b["low"] for b in bars[-21:-1]),
            "resistance_20m": max(b["high"] for b in bars[-21:-1]),
            "breakout_above_previous20m": closes[-1] > max(b["high"] for b in bars[-21:-1]),
            "breakout_below_previous20m": closes[-1] < min(b["low"] for b in bars[-21:-1]),
            "failed_upward_breakout_candidate": bars[-1]["high"]
            > max(b["high"] for b in bars[-21:-1])
            and closes[-1] <= max(b["high"] for b in bars[-21:-1]),
        }
        for minutes, label in (
            (1, "1m"),
            (3, "3m"),
            (5, "5m"),
            (15, "15m"),
            (30, "30m"),
            (60, "1h"),
            (240, "4h"),
        ):
            features["return_" + label] = (
                closes[-1] / closes[-minutes - 1] - 1 if len(closes) > minutes else None
            )
        result.update(
            status="READY",
            reason="Closed historical descriptors only; correlated candidates require chronological incremental-value validation",
            source=data.get("source"),
            received_at=receipt.isoformat(),
            last_closed_at=datetime.fromtimestamp(keys[-1] + 60, timezone.utc).isoformat(),
            closed_bars=len(bars),
            features=features,
            probability_adjustment=0.0,
        )
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result["reason"] = str(exc)
    return result

"""Bounded chart data and immutable signals-only marker construction."""
from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from threading import RLock

from .model import timestamp


SUPPORTED_INTERVALS = {60: "1m", 180: "3m", 300: "5m", 900: "15m"}
MAX_CHART_BARS = 500
MAX_CLOCK_SKEW_SECONDS = 0.25
# Coinbase BTC-USD normally trades every few seconds; a minute with no
# accepted trade means the trade feed is not delivering fresh data.
LIVE_TRADE_STALE_SECONDS = 60


class LiveTradeCandles:
    """Bounded genuine 15-second OHLCV built only from exchange trades."""

    def __init__(self, interval_seconds=15, max_bars=500, max_seen=20_000):
        if interval_seconds != 15 or not 30 <= max_bars <= 2_000:
            raise ValueError("Live chart supports bounded genuine 15-second bars")
        self.interval_seconds = interval_seconds
        self.max_bars = max_bars
        self.bars = {}
        self.seen = set()
        self.seen_order = deque(maxlen=max_seen)
        self.lock = RLock()
        self.metrics = {
            "accepted_trades": 0,
            "duplicate_trades": 0,
            "rejected_trades": 0,
            "out_of_order_trades": 0,
        }
        self.last_event_at = None
        self.last_received_at = None
        self.last_event_second = None
        self.finalized_through = None
        self.event_to_receipt_ms = deque(maxlen=2_000)

    def observe(self, observation):
        try:
            if observation.get("kind") != "trade" or not str(
                observation.get("source", "")
            ).startswith("Coinbase BTC/USD"):
                return False
            event = timestamp(observation["event_at"])
            received = timestamp(observation["received_at"])
            delay = (received - event).total_seconds()
            # Exchange event stamps and local NTP receipts can disagree by a few
            # milliseconds; a receipt materially before its event is rejected.
            if not -MAX_CLOCK_SKEW_SECONDS <= delay <= 5:
                raise ValueError("trade event/receipt timing")
            price = float(observation["price"])
            volume = float(observation["volume"])
            if not math.isfinite(price) or price <= 0 or not math.isfinite(volume) or volume <= 0:
                raise ValueError("trade values")
            identity = (
                observation.get("source"),
                observation.get("session_id"),
                str(observation["trade_id"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
            with self.lock:
                self.metrics["rejected_trades"] += 1
            return False
        event_second = event.timestamp()
        start = int(event_second) // self.interval_seconds * self.interval_seconds
        with self.lock:
            if identity in self.seen:
                self.metrics["duplicate_trades"] += 1
                return False
            if self.finalized_through is not None and start <= self.finalized_through:
                # A late execution must never repaint a bar already published
                # as closed. It remains counted diagnostically, not charted.
                self.metrics["rejected_trades"] += 1
                return False
            if len(self.seen_order) == self.seen_order.maxlen:
                self.seen.discard(self.seen_order[0])
            self.seen_order.append(identity)
            self.seen.add(identity)
            if self.last_event_second is not None and event_second < self.last_event_second:
                self.metrics["out_of_order_trades"] += 1
            self.last_event_second = max(self.last_event_second or event_second, event_second)
            bar = self.bars.get(start)
            if bar is None:
                bar = self.bars[start] = {
                    "start": start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                    "trades": 1,
                    "first_event": event_second,
                    "last_event": event_second,
                }
            else:
                bar["high"] = max(bar["high"], price)
                bar["low"] = min(bar["low"], price)
                bar["volume"] += volume
                bar["trades"] += 1
                if event_second < bar["first_event"]:
                    bar["first_event"], bar["open"] = event_second, price
                if event_second >= bar["last_event"]:
                    bar["last_event"], bar["close"] = event_second, price
            while len(self.bars) > self.max_bars + 2:
                del self.bars[min(self.bars)]
            self.last_event_at = event.isoformat()
            self.last_received_at = received.isoformat()
            self.event_to_receipt_ms.append(delay * 1000)
            self.metrics["accepted_trades"] += 1
        return True

    @staticmethod
    def _percentile(values, fraction):
        if not values:
            return None
        ordered = sorted(values)
        return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)

    def snapshot(self, now, limit=180, finalization_delay_seconds=5):
        current = timestamp(now) if isinstance(now, str) else now
        with self.lock:
            rows = [dict(self.bars[key]) for key in sorted(self.bars)]
            delays = list(self.event_to_receipt_ms)
            metrics = dict(self.metrics)
            last_event_at, last_received_at = self.last_event_at, self.last_received_at
        cutoff = current.timestamp() - finalization_delay_seconds
        closed, forming = [], None
        for row in rows:
            public = {k: v for k, v in row.items() if k not in {"first_event", "last_event"}}
            public["source"] = "Coinbase BTC-USD market_trades WebSocket"
            public["interval_seconds"] = self.interval_seconds
            if row["start"] + self.interval_seconds <= cutoff:
                closed.append(public)
            else:
                forming = {**public, "status": "DEVELOPING", "observed_at": last_received_at}
        if closed:
            with self.lock:
                self.finalized_through = max(
                    self.finalized_through or closed[-1]["start"], closed[-1]["start"]
                )
        age = (
            (current - timestamp(last_received_at)).total_seconds()
            if last_received_at is not None
            else None
        )
        stale = age is not None and age > LIVE_TRADE_STALE_SECONDS
        return {
            "status": "STALE" if stale else "READY" if closed else "WARMING_UP" if forming else "NO_RECORDED_TRADES",
            "stale": stale,
            "stale_after_seconds": LIVE_TRADE_STALE_SECONDS,
            "reason": (
                f"No accepted Coinbase trade for {age:.0f}s; bars are last-known, not live"
                if stale else None
            ),
            "interval": "15s",
            "interval_seconds": 15,
            "bars": closed[-max(30, min(MAX_CHART_BARS, int(limit))):],
            "forming_candle": forming,
            "last_trade_event_at": last_event_at,
            "last_trade_received_at": last_received_at,
            "last_trade_age_seconds": age,
            "finalization_delay_seconds": finalization_delay_seconds,
            "latency": {
                "event_to_receipt_p50_ms": self._percentile(delays, 0.50),
                "event_to_receipt_p95_ms": self._percentile(delays, 0.95),
                "event_to_receipt_p99_ms": self._percentile(delays, 0.99),
                "samples": len(delays),
                "note": "Exchange-event to local socket receipt; includes clock offset and publication delay.",
            },
            "diagnostics": metrics,
            "source": "Genuine timestamped Coinbase BTC-USD market_trades WebSocket executions",
            "non_repainting": "Five-second bounded finalization delay admits late genuine trades; finalized bars are not modified.",
        }


def normalize_recorded_candle(raw):
    """Normalize a real provider candle without changing its time or values.

    Coinbase's JSON APIs encode OHLCV fields as decimal strings. Historical
    journal rows preserve that wire representation, while newer test/replay
    rows may contain numbers. Accept both representations and reject malformed,
    non-finite, unaligned, or non-minute timestamps. Millisecond timestamps are
    deliberately not guessed or rescaled.
    """
    if not isinstance(raw, dict):
        return None
    try:
        start_value = raw["start"]
        if isinstance(start_value, bool):
            return None
        start_number = float(start_value)
        start = int(start_number)
        if not math.isfinite(start_number) or start_number != start:
            return None
        # Bounded Unix seconds: reject milliseconds and clearly invalid dates.
        if not 1_230_768_000 <= start <= 4_102_444_800 or start % 60:
            return None

        values = {}
        for key in ("open", "high", "low", "close"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return None
            value = float(value)
            if not math.isfinite(value):
                return None
            values[key] = value
        if values["low"] > values["high"]:
            return None

        volume = raw.get("volume")
        if volume is None:
            normalized_volume = None
        else:
            if isinstance(volume, bool) or not isinstance(volume, (int, float, str)):
                return None
            normalized_volume = float(volume)
            if not math.isfinite(normalized_volume) or normalized_volume < 0:
                return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None

    return {
        "start": start,
        **values,
        "volume": normalized_volume,
        **({"source": raw["source"]} if raw.get("source") else {}),
    }


def aggregate_candles(candles, interval_seconds):
    """Aggregate closed one-minute bars without inventing sub-minute history."""
    if interval_seconds not in SUPPORTED_INTERVALS:
        raise ValueError("Chart interval is unsupported by recorded candle data")
    buckets = {}
    normalized = [value for raw in candles if (value := normalize_recorded_candle(raw))]
    for raw in sorted(normalized, key=lambda row: row["start"]):
        start = raw["start"]
        bucket = start - start % interval_seconds
        bar = buckets.get(bucket)
        volume = raw.get("volume")
        if bar is None:
            buckets[bucket] = {
                "start": bucket,
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": volume,
                "source_bars": 1,
            }
        else:
            bar["high"] = max(bar["high"], float(raw["high"]))
            bar["low"] = min(bar["low"], float(raw["low"]))
            bar["close"] = float(raw["close"])
            bar["source_bars"] += 1
            bar["volume"] = (
                bar["volume"] + volume if bar["volume"] is not None and volume is not None else None
            )
    return [buckets[key] for key in sorted(buckets)]


def _entry_marker(state):
    dual = state.get("dual_strategy") or {}
    selected = (dual.get("strategies") or {}).get(dual.get("recommended_strategy"))
    if selected and selected.get("actionable") is True:
        market = state.get("market") or {}
        reference = state.get("reference") or {}
        return {
            "marker_key": f"ENTRY:{market.get('ticker')}:{selected['strategy']}:{selected['action']}",
            "ticker": market.get("ticker"), "strategy": selected["strategy"],
            "signal_type": selected["action"], "timestamp": dual.get("timestamp") or state.get("now"),
            "btc_price": reference.get("price"), "contract_price": selected.get("proposed_entry"),
            "target_exit": selected.get("target_exit"), "maximum_potential_loss": selected.get("maximum_potential_loss"),
            "estimated_net_profit": selected.get("estimated_net_profit"), "quantity": selected.get("quantity"),
            "probability": selected.get("confidence"), "after_cost_edge": selected.get("expected_value"),
            "reason": selected.get("reason"), "data_freshness": "FRESH", "risk_status": "VERIFIED_GATES_PASSED",
            "experimental": False,
        }
    edge = state.get("edge_scanner") or {}
    decision = state.get("decision_intelligence") or {}
    guardian = ((decision.get("components") or {}).get("risk_guardian") or {})
    if (
        state.get("stale", True)
        or edge.get("actionable") is not True
        or (edge.get("validation") or {}).get("verified") is not True
        or guardian.get("blockers")
        or guardian.get("signal_kill_switch", {}).get("active")
    ):
        return None
    sides = edge.get("sides") or {}
    qualified = [item for item in sides.values() if item.get("status") == "VERIFIED_POSITIVE_EDGE"]
    if not qualified:
        return None
    chosen = max(qualified, key=lambda item: item.get("net_expected_profit_per_contract", -1))
    side = chosen["side"].lower()
    market = state.get("market") or {}
    reference = state.get("reference") or {}
    signal_type = "BUY UP" if side == "up" else "BUY DOWN"
    return {
        "marker_key": f"ENTRY:{market.get('ticker')}:{signal_type}",
        "ticker": market.get("ticker"),
        "signal_type": signal_type,
        "timestamp": state.get("now"),
        "btc_price": reference.get("price"),
        "contract_price": chosen.get("market_purchase_price"),
        "probability": chosen.get("model_probability"),
        "after_cost_edge": chosen.get("net_expected_profit_per_contract"),
        "reason": chosen.get("reason"),
        "data_freshness": "FRESH",
        "risk_status": guardian.get("status"),
        "experimental": False,
    }


def _exit_markers(state):
    account = state.get("account") or {}
    if account.get("status") != "READ_ONLY_CONNECTED" or state.get("stale", True):
        return []
    reference = state.get("reference") or {}
    output = []
    for item in account.get("guidance") or []:
        side = str(item.get("side", "")).lower()
        if item.get("guidance") != "CONSIDER EXIT" or side not in {"up", "down"}:
            continue
        ticker = item.get("ticker")
        alert = item.get("alert_id")
        if not ticker or alert is None:
            continue
        output.append(
            {
                "marker_key": f"EXIT:{ticker}:{side}:{alert}",
                "ticker": ticker,
                "signal_type": "EXIT UP" if side == "up" else "EXIT DOWN",
                "timestamp": item.get("timestamp") or state.get("now"),
                "btc_price": reference.get("price"),
                "contract_price": item.get("executable_bid"),
                "probability": item.get("forecast_probability"),
                "after_cost_edge": item.get("estimated_impact_after_costs"),
                "reason": item.get("reason"),
                "data_freshness": "FRESH",
                "risk_status": item.get("exit_trigger") or "POSITION_EXIT_REVIEW",
                "experimental": item.get("forecast_assessment") != "UNAVAILABLE",
            }
        )
    return output


def markers_from_state(state):
    """Return only markers justified at this exact recorded state."""
    markers = _exit_markers(state)
    entry = _entry_marker(state)
    if entry:
        markers.append(entry)
    valid = []
    for marker in markers:
        if (
            marker.get("ticker")
            and marker.get("timestamp")
            and type(marker.get("btc_price")) in (int, float)
            and math.isfinite(marker["btc_price"])
        ):
            immutable = {key: marker.get(key) for key in sorted(marker)}
            marker["fingerprint"] = hashlib.sha256(
                json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            valid.append(marker)
    return valid


def marker_is_chronological(marker, now):
    try:
        current = timestamp(now) if isinstance(now, str) else now
        age = (current - timestamp(marker["timestamp"])).total_seconds()
        return 0 <= age <= 30
    except (KeyError, TypeError, ValueError, AttributeError):
        return False

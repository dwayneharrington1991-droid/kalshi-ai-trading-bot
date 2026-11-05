"""Bounded chart data and immutable signals-only marker construction."""
from __future__ import annotations

import hashlib
import json
import math

from .model import timestamp


SUPPORTED_INTERVALS = {60: "1m", 180: "3m", 300: "5m", 900: "15m"}
MAX_CHART_BARS = 500


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

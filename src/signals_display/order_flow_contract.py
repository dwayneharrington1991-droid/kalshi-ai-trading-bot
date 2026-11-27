"""Versioned, read-only payload contract for a live BTC order-flow display.

This module has no network, journal, model, alert, order, or service capability. It
only converts already-normalized, receipt-proven spot snapshots and identified
executions into a bounded JSON-ready document. Displayed depth is kept separate
from executed flow: a disappearing level is not called a cancellation or spoof,
and a depth reduction is not called consumed without separate trade evidence.

The caller remains responsible for exchange-specific checksum/sequence validation.
``connected`` and ``synced`` must both be true before a book can be published as
live. A quiet but integrity-checked book is ``UNCHANGED``; it is not stale merely
because no price level changed during the last second.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from math import isfinite

from .order_flow_verification import (
    Trade,
    book_second,
    spot_depth,
    trade_windows,
    wall_changes,
)


SCHEMA_VERSION = "btc-order-flow-v1"
DISPLAY_LEVELS = (5, 10, 20)
DEFAULT_MAX_AGE_SECONDS = 2.0


def _time(value, name):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite(value, name, *, low=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be finite")
    if low is not None and value < low:
        raise ValueError(f"{name} is below its supported bound")
    return float(value)


def _level(row):
    price, quantity, cumulative, wall = row
    return {
        "price": str(price),
        "quantity_btc": str(quantity),
        "cumulative_btc": str(cumulative),
        "visible_wall": bool(wall),
    }


def _depth_summary(depth):
    out = {}
    for count in DISPLAY_LEVELS:
        bid = sum((row[1] for row in depth["bids"][:count]), Decimal(0))
        ask = sum((row[1] for row in depth["asks"][:count]), Decimal(0))
        total = bid + ask
        out[str(count)] = {
            "bid_btc": str(bid),
            "ask_btc": str(ask),
            "imbalance": str((bid - ask) / total) if total else None,
            "complete": len(depth["bids"]) >= count and len(depth["asks"]) >= count,
        }
    return out


def _largest_wall(rows):
    candidates = [row for row in rows if row[3]]
    if not candidates:
        return None
    price, quantity, cumulative, _wall = max(candidates, key=lambda row: (row[1], -row[0]))
    return {"price": str(price), "quantity_btc": str(quantity), "cumulative_btc": str(cumulative)}


def _wall_events(previous, current, coverage_proven):
    if previous is None:
        return []
    if not coverage_proven:
        return [{"status": "WITHHELD", "reason": "Comparable depth-range coverage was not proven"}]
    output = []
    for side, price, old, new, was_wall, is_wall in wall_changes(previous, current):
        change = (
            "ADDED" if not old else "DISAPPEARED" if not new
            else "GROWN" if new > old else "SHRANK" if new < old else "CLASSIFICATION_CHANGED"
        )
        output.append({
            "side": "bid" if side == "bids" else "ask",
            "price": str(price),
            "previous_btc": str(old),
            "current_btc": str(new),
            "change": change,
            "was_visible_wall": bool(was_wall),
            "is_visible_wall": bool(is_wall),
            "attribution": "DISPLAYED_DEPTH_CHANGE_ONLY",
            "cancellation_inferred": False,
            "spoofing_inferred": False,
            "trade_confirmed_consumption": "UNAVAILABLE_WITHOUT_PRICE_MATCHED_EXECUTIONS",
        })
    return output


def venue_payload(frame, cutoff, *, max_age_seconds=DEFAULT_MAX_AGE_SECONDS):
    """Build one venue document from a normalized, checksum/sequence-gated frame.

    Required keys: ``source``, ``symbol``, ``connected``, ``synced``,
    ``event_at``, ``received_at``, ``last_change_at``, ``bids``, ``asks`` and
    ``trades``. ``previous_bids``/``previous_asks`` are optional. Set
    ``same_depth_coverage`` only when the adapter can prove both snapshots cover
    the same complete range.
    """
    if not isinstance(frame, dict):
        raise ValueError("venue frame must be a mapping")
    source, symbol = frame.get("source"), frame.get("symbol")
    if not isinstance(source, str) or not source or not isinstance(symbol, str) or not symbol:
        raise ValueError("source and symbol are required")
    now = _time(cutoff, "cutoff")
    if frame.get("connected") is not True or frame.get("synced") is not True or frame.get("checksum_verified") is False:
        return {"source": source, "symbol": symbol, "status": "UNAVAILABLE",
                "levels": {"bids": [], "asks": []}, "depth": {}}
    if not frame.get("bids") or not frame.get("asks"):
        return {"source": source, "symbol": symbol, "status": "EMPTY",
                "levels": {"bids": [], "asks": []}, "depth": {}}
    event = _time(frame.get("event_at"), "event_at")
    receipt = _time(frame.get("received_at"), "received_at")
    changed = _time(frame.get("last_change_at"), "last_change_at")
    if event > receipt or receipt > now or changed > receipt:
        raise ValueError("event/change/receipt/cutoff chronology is invalid")
    age_limit = _finite(max_age_seconds, "max_age_seconds", low=0)
    update_state = book_second(
        cutoff=now.timestamp(),
        last_change=changed.timestamp(),
        last_integrity_receipt=receipt.timestamp(),
        max_age=age_limit,
        connected=frame.get("connected") is True,
        synced=frame.get("synced") is True,
    )
    base = {
        "source": source,
        "symbol": symbol,
        "status": "LIVE" if update_state in ("UPDATED", "UNCHANGED") else update_state,
        "update_state": update_state,
        "event_at": event.isoformat(),
        "received_at": receipt.isoformat(),
        "last_change_at": changed.isoformat(),
        "age_seconds": (now - receipt).total_seconds(),
        "feed_latency_ms": (receipt - event).total_seconds() * 1000,
        "sequence": frame.get("sequence"),
        "integrity": {
            "connected": frame.get("connected") is True,
            "sequence_synced": frame.get("synced") is True,
            "checksum_verified": frame.get("checksum_verified"),
        },
        "quantity_unit": "BTC",
        "orders_are_executions": False,
    }
    if update_state not in ("UPDATED", "UNCHANGED"):
        return {**base, "levels": {"bids": [], "asks": []}, "depth": {}}
    depth = spot_depth(frame.get("bids") or [], frame.get("asks") or [])
    previous = None
    if frame.get("previous_bids") is not None or frame.get("previous_asks") is not None:
        previous = spot_depth(frame.get("previous_bids") or [], frame.get("previous_asks") or [])
    trades = frame.get("trades") or []
    if not all(isinstance(trade, Trade) for trade in trades):
        raise ValueError("trades must be identified Trade records")
    coverage_start = frame.get("trade_coverage_start")
    coverage_end = frame.get("trade_coverage_end")
    proof = (coverage_start is not None and coverage_end is not None
             and _time(coverage_start, "trade_coverage_start") <= now
             and _time(coverage_end, "trade_coverage_end") == now)
    windows = trade_windows(trades, now.timestamp()) if proof else {}
    flow = {}
    for seconds, row in windows.items():
        if (now - _time(coverage_start, "trade_coverage_start")).total_seconds() < seconds:
            continue
        total = row["buy_btc"] + row["sell_btc"]
        flow[f"{seconds}s"] = {
            "buy_btc": str(row["buy_btc"]),
            "sell_btc": str(row["sell_btc"]),
            "cvd_change_btc": str(row["cvd_change_btc"]),
            "trade_count": row["count"],
            "trades_per_second": row["trades_per_second"],
            "volume_imbalance": str(row["cvd_change_btc"] / total) if total else None,
        }
    return {
        **base,
        "best_bid": str(depth["bids"][0][0]),
        "best_ask": str(depth["asks"][0][0]),
        "spread": str(depth["spread"]),
        "midpoint": str(depth["midpoint"]),
        "levels": {
            "bids": [_level(row) for row in depth["bids"][:20]],
            "asks": [_level(row) for row in depth["asks"][:20]],
        },
        "depth": _depth_summary(depth),
        "largest_visible_wall": {
            "bid": _largest_wall(depth["bids"]),
            "ask": _largest_wall(depth["asks"]),
        },
        "wall_events": _wall_events(previous, depth, frame.get("same_depth_coverage") is True),
        "executed_flow": {
            "status": "LIVE_EXECUTED_TRADES_ONLY" if flow else "UNAVAILABLE",
            "windows": flow,
            "cvd_definition": "aggressive buy BTC minus aggressive sell BTC; displayed orders excluded",
        },
    }


def dashboard_payload(frames, cutoff, *, kalshi_contract=None):
    """Build the versioned multi-venue payload consumed by a one-second UI.

    Kalshi liquidity, when supplied, remains an opaque separate block and is
    never pooled with BTC spot depth. The caller must build that block from
    fresh executable Kalshi quotes using the existing contract adapter.
    """
    now = _time(cutoff, "cutoff")
    venues = [venue_payload(frame, now) for frame in frames]
    if len({(venue["source"], venue["symbol"]) for venue in venues}) != len(venues):
        raise ValueError("duplicate venue frame")
    live = [venue for venue in venues if venue["status"] == "LIVE"]
    directions = []
    for venue in live:
        row = venue.get("depth", {}).get("20") or {}
        if row.get("complete") and row.get("imbalance") is not None:
            value = Decimal(row["imbalance"])
            directions.append(1 if value > Decimal("0.05") else -1 if value < Decimal("-0.05") else 0)
    mids = [Decimal(venue["midpoint"]) for venue in live]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "status": "LIVE" if live else "UNAVAILABLE",
        "spot_venues": venues,
        "cross_exchange": {
            "status": "AVAILABLE" if len(live) >= 2 else "UNAVAILABLE",
            "venues": [venue["source"] for venue in live],
            "maximum_midpoint_divergence": str(max(mids) - min(mids)) if len(mids) >= 2 else None,
            "depth_pressure": (
                "AGREE" if len(directions) >= 2 and len(set(directions)) == 1
                else "DISAGREE" if len(set(directions)) > 1
                else "INSUFFICIENT_COMPLETE_DEPTH"
            ),
            "predictive_effect": "UNVALIDATED",
        },
        "kalshi_contract": kalshi_contract,
        "semantics": {
            "spot_depth": "Visible spot orders only; not executions, intent, or hidden liquidity",
            "executed_flow": "Identified trades only; never inferred from quote changes",
            "wall_change": "Descriptive displayed-depth change; no spoof/cancel attribution",
            "kalshi": "Separate contract liquidity; never pooled with BTC spot depth",
            "use": "Research display only; not a calibrated forecast or trading instruction",
        },
    }

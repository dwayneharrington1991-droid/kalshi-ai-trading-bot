"""Convert recorded/historical data into ONE chronological event stream.

Each event's ``at`` is the epoch second at which the information became
available to a trader (receipt time for live recordings; exchange time plus a
feed delay for historical prints; candle *end* for candle closes). The engine
processes events strictly in ``at`` order, so no decision can see later data.

Event kinds: market, quote, trade, ai, settlement.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

HIST_TRADE_FEED_DELAY = 0.25  # seconds between an exchange print and our receipt
HIST_SETTLEMENT_DELAY = 120.0  # used only if Kalshi omits settlement_ts


def ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_from_candle(c):
    bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
    ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
    # Kalshi reports bid 0.001/ask 1.0 for an empty side: not an executable quote.
    if bid is None or ask is None or not 0.0 < bid < ask < 1.0:
        return None
    return bid, ask


def historical_market_events(m: dict) -> list[dict]:
    t = m["ticker"]
    opens, closes = ts(m["opens_at"]), ts(m["closes_at"])
    events = [
        {
            "kind": "market",
            "at": opens - 1.0,
            "ticker": t,
            "opens": opens,
            "closes": closes,
            "strike": m.get("strike"),
        }
    ]
    for c in m.get("candles", []):
        q = _quote_from_candle(c)
        end = float(c["end_period_ts"])
        if q is None or not opens < end <= closes:
            continue
        bid, ask = q
        events.append(
            {
                "kind": "quote",
                "at": end,
                "ticker": t,
                "up_bid": bid,
                "up_ask": ask,
                "down_bid": round(1 - ask, 6),
                "down_ask": round(1 - bid, 6),
                "depth": None,  # not published historically; never invented
                "source": "candle_close",
            }
        )
    for tr in m.get("trades_compact", []):
        try:
            exch = ts(tr[0])
            price = float(tr[1])
            count = float(tr[2])
            side = tr[3]
        except (IndexError, TypeError, ValueError):
            continue
        if not opens <= exch < closes or not 0 < price < 1 or count <= 0:
            continue
        if side not in ("yes", "no"):
            continue
        events.append(
            {
                "kind": "trade",
                "at": exch + HIST_TRADE_FEED_DELAY,
                "exchange_time": exch,
                "ticker": t,
                "yes_price": price,
                "count": count,
                "taker_side": side,
            }
        )
    settled_at = ts(m["settlement_ts"]) if m.get("settlement_ts") else closes + HIST_SETTLEMENT_DELAY
    events.append(
        {
            "kind": "settlement",
            "at": max(settled_at, closes),
            "ticker": t,
            "outcome": "up" if m["result"] == "yes" else "down",
        }
    )
    return events


def load_historical(directory: Path, limit: int | None = None) -> tuple[list[dict], dict]:
    files = sorted(Path(directory).glob("KXBTC15M-*.json"))
    markets = []
    for f in files:
        try:
            markets.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    markets.sort(key=lambda m: ts(m["opens_at"]))
    if limit:
        markets = markets[-limit:]
    events = []
    truncated = 0
    for m in markets:
        events.extend(historical_market_events(m))
        truncated += bool(m.get("trades_truncated"))
    events.sort(key=lambda e: (e["at"], _ORDER[e["kind"]]))
    info = {
        "source": "historical",
        "markets": len(markets),
        "markets_with_truncated_trade_tape": truncated,
        "first": markets[0]["opens_at"] if markets else None,
        "last": markets[-1]["closes_at"] if markets else None,
        "depth_available": False,
    }
    return events, info


def _depth_from_book(yes_bids, no_bids):
    yes_bids = [(float(p), float(q)) for p, q in yes_bids]
    no_bids = [(float(p), float(q)) for p, q in no_bids]
    return {
        "up_bids": sorted(yes_bids, reverse=True),
        "down_bids": sorted(no_bids, reverse=True),
        # Buying UP lifts resting NO bids at (1 - p), and vice versa.
        "up_asks": sorted((round(1 - p, 6), q) for p, q in no_bids),
        "down_asks": sorted((round(1 - p, 6), q) for p, q in yes_bids),
    }


def record_to_event(r: dict):
    """One recorder line -> one event (or None). Used by replay and live mode."""
    kind = r.get("type")
    try:
        at = ts(r["received_at"])
    except (KeyError, ValueError, TypeError):
        return None
    if kind == "market":
        return {
            "kind": "market",
            "at": at,
            "ticker": r["ticker"],
            "opens": ts(r["opens_at"]),
            "closes": ts(r["closes_at"]),
            "strike": r.get("strike"),
        }
    if kind == "book":
        depth = _depth_from_book(r.get("yes_bids") or [], r.get("no_bids") or [])
        if not depth["up_bids"] or not depth["down_bids"]:
            return None  # one-sided book: no executable two-way quote
        return {
            "kind": "quote",
            "at": at,
            "ticker": r["ticker"],
            "up_bid": depth["up_bids"][0][0],
            "up_ask": depth["up_asks"][0][0],
            "down_bid": depth["down_bids"][0][0],
            "down_ask": depth["down_asks"][0][0],
            "depth": depth,
            "source": "orderbook",
        }
    if kind == "trade":
        return {
            "kind": "trade",
            "at": at,
            "exchange_time": ts(r["created_time"]),
            "ticker": r["ticker"],
            "yes_price": float(r["yes_price"]),
            "count": float(r["count"]),
            "taker_side": r["taker_side"],
        }
    if kind == "ai":
        return ai_event(r, at)
    if kind == "settlement":
        return {"kind": "settlement", "at": at, "ticker": r["ticker"], "outcome": r["outcome"]}
    return None


def recorded_events(path: Path) -> tuple[list[dict], dict]:
    events, counts, settled = [], {}, set()
    for line in Path(path).read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            counts["malformed"] = counts.get("malformed", 0) + 1
            continue
        counts[r.get("type")] = counts.get(r.get("type"), 0) + 1
        try:
            e = record_to_event(r)
        except (KeyError, TypeError, ValueError):
            counts["invalid"] = counts.get("invalid", 0) + 1
            continue
        if e is None:
            continue
        if e["kind"] == "settlement":
            if e["ticker"] in settled:
                counts["duplicate_settlement"] = counts.get("duplicate_settlement", 0) + 1
                continue  # only the first published result counts
            settled.add(e["ticker"])
        events.append(e)
    events.sort(key=lambda e: (e["at"], _ORDER[e["kind"]]))
    info = {"source": "recorded_live", "file": str(path), "record_counts": counts, "depth_available": True}
    return events, info


def ai_event(r: dict, at: float):
    """Existing AI outputs, only if generated no later than receipt."""
    live, opening = r.get("live") or {}, r.get("opening")
    live_up = None
    try:
        if (
            not r.get("stale")
            and live.get("up") is not None
            and ts(live["data_cutoff"]) <= at
            and ts(live["updated_at"]) <= at
        ):
            live_up = float(live["up"])
    except (KeyError, TypeError, ValueError):
        live_up = None
    opening_up = None
    opening_ticker = None
    try:
        if opening and opening.get("up") is not None and ts(opening["generated_at"]) <= at:
            opening_up = float(opening["up"])
            opening_ticker = opening.get("ticker")
    except (KeyError, TypeError, ValueError):
        opening_up = None
    return {
        "kind": "ai",
        "at": at,
        "ticker": r.get("ticker"),
        "live_up": live_up,
        "opening_up": opening_up,
        "opening_ticker": opening_ticker,
        "regime": live.get("regime"),
    }


# Same-instant ordering: metadata, then evidence, then outcome.
_ORDER = {"market": 0, "quote": 1, "trade": 2, "ai": 3, "settlement": 4}

"""Read-only probe: is an executable Kalshi quote observable BEFORE a KXBTC15M opening?

Polls Kalshi's public, unauthenticated market-data endpoints (the same ones the
collector already uses) for a bounded time, and for each upcoming contract
records, with raw receipt timestamps:

* the listed status and official ``open_time``/``close_time``;
* the listing's yes/no bid and ask (``*_dollars`` fields, as published);
* near the opening, the public order book's best levels and depth.

Every row is labelled OBSERVED (published by Kalshi at the receipt time) and
nothing is inferred or interpolated. A quote counts as executable only when the
market status is open/active and the ask is strictly between 0 and 1 with a
positive size. No credentials, account data or orders are involved; nothing
in the bot, its journal or Oracle services is touched. Output is a JSON report
in the research directory.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PROBE_FORMAT = "kxbtc-preopen-quote-probe-v1"
DEFAULT_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TRADABLE = {"open", "active"}
BOOK_WINDOW_SECONDS = 90  # fetch depth only near an opening, to stay light


def _now():
    return datetime.now(timezone.utc)


def _get(base, path, timeout=8):
    request = Request(base + path, headers={"Accept": "application/json", "User-Agent": "kxbtc-research-probe"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _dollars(raw, name):
    try:
        value = float(raw[name + "_dollars"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _levels(levels, scale):
    """[(price_dollars, size)] with positive size, mirroring the collector's parser."""
    out = []
    for level in levels or []:
        try:
            price, size = float(level[0]) * scale, float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(price) and math.isfinite(size) and 0 <= price <= 1 and size > 0:
            out.append((price, size))
    return out


def _best(levels):
    if not levels:
        return None, 0.0, 0.0
    price = max(p for p, _ in levels)
    return price, sum(q for p, q in levels if p == price), sum(q for _, q in levels)


def book_snapshot(base, ticker):
    """Public order book -> best bids, implied asks and depth for YES/NO.

    Same two formats as the collector: ``orderbook_fp`` in dollars, legacy
    ``orderbook`` in cents. A YES ask is the complement of the best NO bid,
    executable up to the size resting at that NO bid (and vice versa)."""
    book = _get(base, "/markets/" + quote(ticker, safe="") + "/orderbook")
    if "orderbook_fp" in book:
        fixed = book["orderbook_fp"] or {}
        yes, no = _levels(fixed.get("yes_dollars"), 1.0), _levels(fixed.get("no_dollars"), 1.0)
    else:
        legacy = book.get("orderbook") or {}
        yes, no = _levels(legacy.get("yes"), 0.01), _levels(legacy.get("no"), 0.01)
    yes_bid, yes_best_size, yes_depth = _best(yes)
    no_bid, no_best_size, no_depth = _best(no)
    return {
        "yes_bid": yes_bid, "no_bid": no_bid,
        "yes_ask": None if no_bid is None else round(1 - no_bid, 6),
        "no_ask": None if yes_bid is None else round(1 - yes_bid, 6),
        "yes_ask_size": no_best_size, "no_ask_size": yes_best_size,
        "yes_bid_depth": yes_depth, "no_bid_depth": no_depth,
        "spread_yes": None if yes_bid is None or no_bid is None else round(1 - no_bid - yes_bid, 6),
    }


def observe(base, now, series="KXBTC15M"):
    params = {"series_ticker": series, "min_close_ts": int(now.timestamp()),
              "max_close_ts": int(now.timestamp()) + 2700, "limit": 50}
    listing = _get(base, "/markets?" + urlencode(params))
    received = _now()
    rows = []
    for raw in listing.get("markets", []):
        try:
            close = _parse_time(raw["close_time"])
            official_open = _parse_time(raw["open_time"])
        except (KeyError, TypeError, ValueError):
            continue
        contract_open = max(official_open, close - timedelta(minutes=15))
        seconds_to_open = (contract_open - received).total_seconds()
        row = {
            "kind": "OBSERVED", "ticker": raw.get("ticker"), "received_at": received.isoformat(),
            "status": raw.get("status"), "open_time": raw.get("open_time"), "close_time": raw.get("close_time"),
            "contract_open": contract_open.isoformat(), "seconds_to_open": seconds_to_open,
            "listing": {k: _dollars(raw, k) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask")},
        }
        if -BOOK_WINDOW_SECONDS <= seconds_to_open <= BOOK_WINDOW_SECONDS:
            try:
                row["book"] = book_snapshot(base, raw["ticker"])
                row["book_received_at"] = _now().isoformat()
            except Exception as exc:  # recorded, never inferred
                row["book_error"] = type(exc).__name__
        row["executable_before_open"] = executable(row) and seconds_to_open > 0
        row["listed_ask_without_depth_before_open"] = (
            not row["executable_before_open"] and listed_ask(row) and seconds_to_open > 0)
        rows.append(row)
    return rows


def executable(row):
    if row.get("status") not in TRADABLE:
        return False
    book = row.get("book") or {}
    for ask, depth in ((book.get("yes_ask"), book.get("yes_ask_size")), (book.get("no_ask"), book.get("no_ask_size"))):
        if ask is not None and 0 < ask < 1 and (depth or 0) > 0:
            return True
    return False


def listed_ask(row):
    """A published listing ask in (0, 1) without verified depth: weaker evidence."""
    if row.get("status") not in TRADABLE:
        return False
    listing = row.get("listing") or {}
    return any(v is not None and 0 < v < 1 for v in (listing.get("yes_ask"), listing.get("no_ask")))


def summarize(rows):
    by_ticker = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row)
    contracts = {}
    for ticker, items in by_ticker.items():
        pre = [r for r in items if r["seconds_to_open"] > 0]
        contracts[ticker] = {
            "observations": len(items),
            "statuses_before_open": sorted({str(r["status"]) for r in pre}),
            "earliest_executable_seconds_to_open": max(
                (r["seconds_to_open"] for r in pre if r["executable_before_open"]), default=None),
            "listed_ask_without_depth_before_open": any(r["listed_ask_without_depth_before_open"] for r in pre),
            "official_open_time": items[0]["open_time"], "contract_open": items[0]["contract_open"],
        }
    observed_openings = [c for c in contracts.values() if c["statuses_before_open"]]
    any_pre = any(c["earliest_executable_seconds_to_open"] is not None for c in contracts.values())
    return {
        "contracts": contracts,
        "contracts_observed_before_their_open": len(observed_openings),
        "executable_quote_seen_before_any_open": any_pre,
        "conclusion": (
            "PRE_OPEN_EXECUTABLE_QUOTES_EXIST: the collector can capture them (check its status filter and timing)"
            if any_pre else
            "NO_PRE_OPEN_EXECUTABLE_QUOTE_OBSERVED: during this probe no upcoming contract was tradable with a "
            "positive-size ask before its open; the earliest executable price is after the opening"
            if observed_openings else
            "INCONCLUSIVE: no contract was observed before its opening during the probe window"
        ),
    }


def run(base, duration, interval, sleep=time.sleep, clock=_now):
    rows, errors = [], {}
    end = clock() + timedelta(seconds=duration)
    while clock() < end:
        try:
            rows.extend(observe(base, clock()))
        except Exception as exc:
            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
        sleep(interval)
    return {"format": PROBE_FORMAT, "role": "READ_ONLY_PUBLIC_MARKET_DATA_PROBE", "base": base,
            "duration_seconds": duration, "interval_seconds": interval, "errors": errors,
            "observations": rows, "summary": summarize(rows),
            "note": "OBSERVED rows are Kalshi publications at their receipt time; nothing is inferred."}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, default=1200)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--base", default=DEFAULT_BASE)
    args = parser.parse_args()
    if not 60 <= args.duration_seconds <= 3600 or not 5 <= args.interval_seconds <= 120:
        parser.error("duration must be 60-3600 s and interval 5-120 s (light, bounded polling)")
    if args.output.exists():
        parser.error(f"Refusing to overwrite {args.output}")
    report = run(args.base, args.duration_seconds, args.interval_seconds)
    from .model_registry import atomic_json

    atomic_json(args.output, report)
    s = report["summary"]
    print("PREOPEN_PROBE_BEGIN")
    print(f"PROBE contracts_observed_before_open={s['contracts_observed_before_their_open']} "
          f"executable_before_open={s['executable_quote_seen_before_any_open']} errors={report['errors']}")
    for ticker, c in s["contracts"].items():
        print(f"  {ticker} statuses_before_open={c['statuses_before_open']} official_open={c['official_open_time']} "
              f"earliest_executable_s_to_open={c['earliest_executable_seconds_to_open']}")
    print(f"PROBE_CONCLUSION {s['conclusion']}")
    print("PREOPEN_PROBE_END")


if __name__ == "__main__":
    main()

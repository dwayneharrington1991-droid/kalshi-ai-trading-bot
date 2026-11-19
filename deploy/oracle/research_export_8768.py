#!/usr/bin/env python3
"""Read-only export of distinct Coinbase BTC/USD price updates for the 30-second research harness.

Runs on the Oracle server with the standard library only:

    python3 -I research_export_8768.py --db JOURNAL --hours 72 --out-dir NEW_DIR

Guarantees
  * The journal is opened with SQLite mode=ro and query_only=1. It is never written, and
    read-only readers do not block the live writer in WAL mode.
  * Only one source, one time window and a row cap are read. The whole journal is never exported.
  * --out-dir must NOT exist. Nothing is overwritten.
  * Availability is LOCAL, not exchange time. Each update becomes usable at the latest of its
    receipt, observed and row timestamps. The exchange event time is recorded for audit but is
    never used to decide what was known at a given moment, because it can precede receipt.

Outputs in NEW_DIR
  coinbase_updates.csv  one row per distinct valid price update (only the needed fields)
  summary.json          counts, spacing, gaps, duplicates, key names found, and the CSV sha256
"""
import argparse
import csv
import hashlib
import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

SOURCE = "Coinbase BTC/USD stream"
RECEIVED_KEYS = ("received_at", "quote_received_at", "local_received_at", "receipt_at")
EVENT_KEYS = ("event_time", "event_at")
COLUMNS = ["available_at_utc", "event_at_utc", "price", "bid", "ask", "bid_size", "ask_size",
           "source", "row_timestamp_utc"]
MAX_ROWS = 300000
GAP_S = 60


def parse_ts(text):
    dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt.astimezone(timezone.utc)


def finite(value):
    if value is None:
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("non-finite number")
    return number


def first(data, keys):
    for key in keys:
        if data.get(key) is not None:
            return key, data[key]
    return None, None


def parse_row(row_ts, payload):
    obj = json.loads(payload)
    data = obj["data"] if isinstance(obj.get("data"), dict) else obj
    price = finite(data.get("price"))
    if price is None or price <= 0:
        return None, "non_positive_price"
    received_key, received = first(data, RECEIVED_KEYS)
    _, observed = first(data, ("observed_at",))
    event_key, event = first(data, EVENT_KEYS)
    times = [parse_ts(row_ts)]
    if observed is not None:
        times.append(parse_ts(observed))
    if received is not None:
        times.append(parse_ts(received))
    available = max(times)                       # never earlier than any local evidence of receipt
    record = {
        "available_at": available,
        "event_at": parse_ts(event) if event is not None else None,
        "price": price,
        "bid": finite(data.get("bid")),
        "ask": finite(data.get("ask")),
        "bid_size": finite(data.get("bid_size")),
        "ask_size": finite(data.get("ask_size")),
        "row_ts": parse_ts(row_ts),
        "has_receipt": received_key is not None,
        "has_event": event_key is not None,
    }
    if record["bid"] is not None and record["ask"] is not None and record["bid"] > record["ask"]:
        return None, "crossed_quote"
    return record, None


def export(db_path, since, until, out_dir, max_rows=MAX_ROWS):
    if os.path.exists(out_dir):
        raise SystemExit("refused: output folder already exists")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only = 1")
        db.execute("PRAGMA busy_timeout = 5000")
        pad = (since - timedelta(minutes=2)).isoformat()
        end = (until + timedelta(minutes=2)).isoformat()
        count = db.execute("SELECT COUNT(*) FROM feed_observations WHERE source=? AND timestamp>=? AND timestamp<?",
                           (SOURCE, pad, end)).fetchone()[0]
        if count > max_rows:
            raise SystemExit(f"refused: {count} rows exceeds the cap of {max_rows}; use a shorter window")
        cursor = db.execute("SELECT timestamp, payload FROM feed_observations "
                            "WHERE source=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                            (SOURCE, pad, end))
        scanned, rejected, parsed = 0, {}, []
        for row_ts, payload in cursor:
            scanned += 1
            try:
                record, reason = parse_row(row_ts, payload)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                record, reason = None, "unparseable"
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            if since <= record["available_at"] <= until:
                parsed.append(record)
    finally:
        db.close()

    parsed.sort(key=lambda r: r["available_at"])
    kept, dup_identical, dup_time = [], 0, 0
    for r in parsed:
        if kept and kept[-1]["available_at"] == r["available_at"]:
            dup_time += 1
            continue
        if kept:
            prev = kept[-1]
            same = (prev["price"], prev["bid"], prev["ask"], prev["bid_size"], prev["ask_size"],
                    prev["event_at"]) == (r["price"], r["bid"], r["ask"], r["bid_size"], r["ask_size"], r["event_at"])
            if same:
                dup_identical += 1
                continue
        kept.append(r)

    os.makedirs(out_dir)
    csv_path = os.path.join(out_dir, "coinbase_updates.csv")
    with open(csv_path, "x", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for r in kept:
            writer.writerow([
                r["available_at"].isoformat(), r["event_at"].isoformat() if r["event_at"] else "",
                r["price"], "" if r["bid"] is None else r["bid"], "" if r["ask"] is None else r["ask"],
                "" if r["bid_size"] is None else r["bid_size"], "" if r["ask_size"] is None else r["ask_size"],
                SOURCE, r["row_ts"].isoformat()])
    with open(csv_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()

    gaps = [(b["available_at"] - a["available_at"]).total_seconds() for a, b in zip(kept, kept[1:])]
    gaps_sorted = sorted(gaps)
    long_gaps = [g for g in gaps if g > GAP_S]
    summary = {
        "source": SOURCE, "window_utc": [since.isoformat(), until.isoformat()],
        "rows_scanned_in_padded_window": scanned, "rejected": rejected,
        "distinct_updates_exported": len(kept),
        "duplicates_identical_removed": dup_identical, "duplicates_same_time_removed": dup_time,
        "receipt_field_present": sum(1 for r in kept if r["has_receipt"]),
        "event_field_present": sum(1 for r in kept if r["has_event"]),
        "first_available_utc": kept[0]["available_at"].isoformat() if kept else None,
        "last_available_utc": kept[-1]["available_at"].isoformat() if kept else None,
        "median_gap_s": round(statistics.median(gaps), 3) if gaps else None,
        "p90_gap_s": round(gaps_sorted[int(len(gaps_sorted) * 0.9)], 3) if gaps else None,
        "max_gap_s": round(max(gaps), 3) if gaps else None,
        "gaps_over_60s": len(long_gaps), "seconds_in_gaps_over_60s": round(sum(long_gaps), 1),
        "availability_rule": "max(row timestamp, observed_at, receipt) - never the exchange event time",
        "csv_sha256": digest, "columns": COLUMNS,
    }
    with open(os.path.join(out_dir, "summary.json"), "x") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps({k: summary[k] for k in ("distinct_updates_exported", "median_gap_s", "max_gap_s",
                                              "gaps_over_60s", "csv_sha256")}, sort_keys=True))


def main(argv=None):
    ap = argparse.ArgumentParser(description="read-only Coinbase update export")
    ap.add_argument("--db", required=True)
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--since", help="ISO UTC; default now - hours")
    ap.add_argument("--until", help="ISO UTC; default now")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-rows", type=int, default=MAX_ROWS)
    args = ap.parse_args(argv)
    until = parse_ts(args.until) if args.until else datetime.now(timezone.utc)
    since = parse_ts(args.since) if args.since else until - timedelta(hours=args.hours)
    if until <= since or (until - since) > timedelta(days=14):
        raise SystemExit("refused: window must be positive and at most 14 days")
    export(args.db, since, until, args.out_dir, args.max_rows)


if __name__ == "__main__":
    main()

"""Read-only adapter: stored BTC observations -> contiguous 30-second segments for the research harness.

Read-only by construction: the journal is opened with SQLite mode=ro and nothing is written.
No production table, schema or service is touched.

Sources (what the live collector actually persists):
  * feed_observations, source "Coinbase BTC/USD proxy": dashboard coverage snapshots. The price
    and its exchange observed_at are in the payload. Spacing depends on the refresh cadence,
    so the adapter reports the spacing it finds instead of assuming it.
  * btc_candles, source "Coinbase...": 60-second OHLCV bars. Too coarse for 30-second moves;
    included only for the coverage report, never used as a 30-second price.

Causality: the grid price at time t is the last observation with time <= t. A grid point is
usable only if that observation is no older than MAX_STALE_S. Stale or empty points break the
segment; they are never filled. The label for t uses the grid point at t+30 and only forms
inside one contiguous segment.
"""
import argparse
import bisect
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import thirty_second_research as tsr

STEP_S = 30
MAX_STALE_S = 60
PRICE_SOURCE = "Coinbase BTC/USD proxy"


def open_readonly(path):
    return sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)


def _epoch(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def price_points(db, source=PRICE_SOURCE):
    """Sorted (epoch, price) from stored coverage snapshots. Rows without a usable price are counted."""
    points, skipped = [], 0
    for (payload,) in db.execute("SELECT payload FROM feed_observations WHERE source=?", (source,)):
        try:
            data = json.loads(payload).get("data") or {}
            price = float(data["price"])
            at = _epoch(data["observed_at"])
            if price > 0:
                points.append((at, price))
            else:
                skipped += 1
        except (KeyError, TypeError, ValueError, AttributeError):
            skipped += 1
    points.sort()
    return points, skipped


def candle_coverage(db):
    row = db.execute("SELECT COUNT(*), MIN(start), MAX(start) FROM btc_candles WHERE source LIKE 'Coinbase%'").fetchone()
    return {"bars": row[0], "first_start": row[1], "last_start": row[2], "bar_seconds": 60}


def grid_segments(points, step=STEP_S, max_stale=MAX_STALE_S):
    """Causal 30-second grid. Returns (segments, stats). Each segment is a contiguous list of bars."""
    if not points:
        return [], {"grid_points": 0, "usable_points": 0, "segments": 0}
    times = [t for t, _ in points]
    prices = [p for _, p in points]
    start = int(times[0] // step + 1) * step          # first grid point after the first observation
    end = int(times[-1] // step) * step
    segments, current, stale, total = [], [], 0, 0
    t = start
    while t <= end:
        total += 1
        k = bisect.bisect_right(times, t) - 1         # last observation at or before t
        if k >= 0 and t - times[k] <= max_stale:
            current.append({"ts": t, "close": prices[k], "volume": None})
        else:
            stale += 1
            if current:
                segments.append(current)
                current = []
        t += step
    if current:
        segments.append(current)
    usable = sum(len(s) for s in segments)
    return segments, {"grid_points": total, "usable_points": usable, "stale_points": stale,
                      "segments": len(segments)}


def spacing_report(points):
    if len(points) < 2:
        return {"observations": len(points)}
    gaps = sorted(b[0] - a[0] for a, b in zip(points, points[1:]))
    med = gaps[len(gaps) // 2]
    return {"observations": len(points), "first": points[0][0], "last": points[-1][0],
            "median_gap_s": round(med, 2), "p90_gap_s": round(gaps[int(len(gaps) * 0.9)], 2),
            "max_gap_s": round(gaps[-1], 2)}


def export(journal_path, *, max_stale=MAX_STALE_S):
    db = open_readonly(journal_path)
    try:
        points, skipped = price_points(db)
        candles = candle_coverage(db)
    finally:
        db.close()
    segments, grid = grid_segments(points, max_stale=max_stale)
    report = {"price_source": PRICE_SOURCE, "skipped_rows": skipped, "spacing": spacing_report(points),
              "grid": grid, "candles_60s": candles, "volume_available": False}
    result = tsr.run_segments(segments) if segments else {"verdict": "NO_VALIDATED_EDGE",
                                                           "reason": "no usable 30-second grid points"}
    report["research"] = result
    return report


def _main(argv=None):
    ap = argparse.ArgumentParser(description="read-only 30-second research export (no writes)")
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="optional report path (written as a new file)")
    args = ap.parse_args(argv)
    report = export(args.journal)
    text = json.dumps(report, indent=1, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "x") as f:                # refuses to overwrite an existing report
            f.write(text)
    print(text)


if __name__ == "__main__":
    _main()

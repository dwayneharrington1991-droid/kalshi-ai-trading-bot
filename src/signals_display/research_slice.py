"""Export a compact, read-only research slice of a journal for OFF-SERVER evaluation.

The slice is a new SQLite file with the SAME table definitions, holding only the
rows and JSON fields that the opening research loaders read:

* forecast_outcomes: every row;
* btc_engine_opening: every PUBLIC MARKET DATA row (immutable, unchanged);
* opening_quote_snapshots: rows within the loaders' windows around each
  opening (payload column, never read by the loaders, replaced by '{}');
* predictions: the first five rows within 60 s after each opening call (cost
  quotes) and the first row of each settled contract the retrospective loader
  considers, with the payload reduced to exactly the JSON paths the loaders
  extract (stale flag, quotes, fee multiplier, contract times, strike and
  settlement-rule flags);
* btc_candles: Coinbase rows inside the retrospective 24-hour windows.

The source journal is opened read-only (mode=ro, query_only); nothing in it is
changed. Tests prove the loaders return identical rows from the slice and from
the full journal.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .model import timestamp

TABLES = ("forecast_outcomes", "btc_engine_opening", "opening_quote_snapshots", "predictions", "btc_candles")
RETRO_LIMIT = 3000          # opening_research.RETROSPECTIVE_LIMIT
RETRO_MARGIN = 2000         # extra recent contracts so earlier pinned as-of times are fully covered
QUOTE_WINDOW = 60           # opening_research.QUOTE_WINDOW_SECONDS
PREOPEN_WINDOW = 60         # opening_research.PREOPEN_QUOTE_WINDOW_SECONDS
PATHS = ("$.stale", "$.market.quotes.up.bid", "$.market.quotes.up.ask", "$.market.quotes.down.ask",
         "$.market.fee_multiplier", "$.market.opens_at", "$.market.closes_at", "$.market.strike",
         "$.market.mapping_verified", "$.market.comparison_operator", "$.market.averaging_rule_verified")
EXTRACT = ",".join(f"json_extract(payload,'{p}')" for p in PATHS)


def _reduced(values):
    """The loaders' JSON paths only, written by Python's json (exact number round trip;
    SQLite's json_object would print reals with 15 significant digits)."""
    (stale, up_bid, up_ask, down_ask, fee, opens, closes, strike, mapped, operator, averaging) = values
    return json.dumps({"stale": stale, "market": {
        "quotes": {"up": {"bid": up_bid, "ask": up_ask}, "down": {"ask": down_ask}}, "fee_multiplier": fee,
        "opens_at": opens, "closes_at": closes, "strike": strike, "mapping_verified": mapped,
        "comparison_operator": operator, "averaging_rule_verified": averaging}})


def _columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def export(source, out, asof=None, progress=print):
    asof = asof or datetime.now(timezone.utc)
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; refusing to overwrite")
    src = sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True, timeout=30)
    src.execute("PRAGMA query_only=ON")
    dst = sqlite3.connect(out)
    counts = {}
    with closing(src), closing(dst):
        present = {n for (n,) in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table in present:
                dst.execute(src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0])
        cols = {t: _columns(src, t) for t in TABLES if t in present}

        def put(table, rows):
            marks = ",".join("?" * len(cols[table]))
            dst.executemany(f"INSERT OR IGNORE INTO {table} ({','.join(cols[table])}) VALUES ({marks})", rows)
            counts[table] = counts.get(table, 0) + len(rows)

        if "forecast_outcomes" in present:
            put("forecast_outcomes", src.execute("SELECT * FROM forecast_outcomes").fetchall())
        openings = []
        if "btc_engine_opening" in present:
            rows = src.execute("SELECT * FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA'").fetchall()
            put("btc_engine_opening", rows)
            for ticker, generated, opens in src.execute(
                    "SELECT ticker,generated_at,json_extract(payload,'$.opens_at') FROM btc_engine_opening "
                    "WHERE mode='PUBLIC MARKET DATA'"):
                openings.append((ticker, generated, opens))
        progress(f"openings {len(openings)}")
        if "opening_quote_snapshots" in present:
            c = cols["opening_quote_snapshots"]
            select = ",".join("'{}'" if name == "payload" else name for name in c)
            for ticker, generated, opens in openings:
                try:
                    lo = min(timestamp(opens) - timedelta(seconds=PREOPEN_WINDOW), timestamp(generated))
                    hi = max(timestamp(opens), timestamp(generated) + timedelta(seconds=QUOTE_WINDOW))
                except (TypeError, ValueError):
                    continue
                put("opening_quote_snapshots", src.execute(
                    f"SELECT {select} FROM opening_quote_snapshots WHERE mode='PUBLIC MARKET DATA' AND ticker=? "
                    "AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?)",
                    (ticker, lo.isoformat(), hi.isoformat())).fetchall())
        windows = []
        if "predictions" in present:
            c = cols["predictions"]
            k = c.index("payload")
            select = ",".join(EXTRACT if name == "payload" else name for name in c)

            def rows_of(cursor):
                return [tuple(r[:k]) + (_reduced(r[k:k + len(PATHS)]),) + tuple(r[k + len(PATHS):]) for r in cursor]

            for ticker, generated, _opens in openings:
                try:
                    called = timestamp(generated)
                except (TypeError, ValueError):
                    continue
                put("predictions", rows_of(src.execute(
                    f"SELECT {select} FROM predictions WHERE ticker=? AND mode='PUBLIC MARKET DATA' "
                    "AND julianday(timestamp)>=julianday(?) AND julianday(timestamp)<=julianday(?) "
                    "ORDER BY timestamp LIMIT 5",
                    (ticker, called.isoformat(), (called + timedelta(seconds=QUOTE_WINDOW)).isoformat()))))
            if "forecast_outcomes" in present:
                considered = src.execute(
                    "SELECT ticker FROM forecast_outcomes WHERE origin='official' AND julianday(observed_at)<=julianday(?) "
                    "ORDER BY observed_at DESC LIMIT ?", (asof.isoformat(), RETRO_LIMIT * 2 + RETRO_MARGIN)).fetchall()
                for (ticker,) in considered:
                    row = rows_of(src.execute(f"SELECT {select} FROM predictions WHERE ticker=? AND mode='PUBLIC MARKET DATA' "
                                              "ORDER BY timestamp LIMIT 1", (ticker,)))
                    if row:
                        put("predictions", row)
                        opens = json.loads(row[0][k])["market"].get("opens_at")
                        try:
                            windows.append(int(timestamp(opens).timestamp()))
                        except (TypeError, ValueError):
                            pass
        progress(f"retrospective contracts with metadata {len(windows)}")
        if "btc_candles" in present and windows:
            first, last = min(windows) - 86520, max(windows)
            for (source,) in src.execute("SELECT source FROM btc_candles GROUP BY source").fetchall():
                if str(source).startswith("Coinbase"):
                    put("btc_candles", src.execute("SELECT * FROM btc_candles WHERE source=? AND start>=? AND start<=?",
                                                   (source, first, last)).fetchall())
        dst.commit()
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    manifest = {"format": "kxbtc-research-slice-v1", "asof": asof.isoformat(), "counts": counts,
                "bytes": out.stat().st_size, "sha256": digest}
    Path(str(out) + ".manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asof", required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.journal.resolve() or args.output.suffix != ".db":
        parser.error("output must be a new, separate .db file")
    manifest = export(args.journal, args.output, datetime.fromisoformat(args.asof))
    print("SLICE", json.dumps(manifest))


if __name__ == "__main__":
    main()


def reassemble(part_paths, out_dir):
    """Rebuild bundle.tgz from uploaded 'KXSLICE i n sha256' text parts, verify its
    SHA-256 and unpack it into out_dir. Refuses missing, duplicate or mismatched parts."""
    import base64
    import tarfile
    parts, total, digest = {}, None, None
    for path in part_paths:
        head, _, body = Path(path).read_text().partition("\n")
        tag, i, n, s = head.split()
        if tag != "KXSLICE" or (total and int(n) != total) or (digest and s != digest) or int(i) in parts:
            raise ValueError(f"unexpected or duplicate part {path}")
        total, digest, parts[int(i)] = int(n), s, body.strip()
    if total is None or sorted(parts) != list(range(1, total + 1)):
        raise ValueError(f"missing parts: have {sorted(parts)} of {total}")
    data = base64.b64decode("".join(parts[i] for i in range(1, total + 1)))
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("bundle SHA-256 mismatch")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bundle.tgz").write_bytes(data)
    with tarfile.open(out / "bundle.tgz") as tar:
        names = tar.getnames()
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise ValueError("unsafe path in bundle")
        tar.extractall(out, filter="data")
    return {"parts": total, "sha256": digest, "files": names}

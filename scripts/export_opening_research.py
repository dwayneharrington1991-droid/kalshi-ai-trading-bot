"""Read-only export of the tables the pre-registered opening test needs (research only).

Run ON the Oracle host with the bot's Python; it opens the journal with SQLite mode=ro and
PRAGMA query_only, writes JSON to stdout and changes nothing:

  nice -n 19 python - < scripts/export_opening_research.py > journal_dump.json

The journal defaults to the live 8768 journal; pass --journal to read another copy.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing

DEFAULT_JOURNAL = "/home/opc/kalshi-preview-upgrade/logs/upgraded-live-8768.db"
TABLES = {
    "btc_engine_opening": "SELECT * FROM btc_engine_opening",
    "forecast_outcomes": "SELECT * FROM forecast_outcomes",
    "opening_quote_snapshots": "SELECT * FROM opening_quote_snapshots WHERE seconds_from_open BETWEEN -5 AND 30",
}


def export(journal):
    out = {}
    with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        for name, query in TABLES.items():
            cols = [r[1] for r in db.execute(f"PRAGMA table_info({name})")]
            out[name] = {"cols": cols, "rows": db.execute(query).fetchall()}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    args = ap.parse_args(argv)
    json.dump(export(args.journal), sys.stdout, default=str)


if __name__ == "__main__":
    main()

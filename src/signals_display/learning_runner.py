"""Offline learning run for 8768: ledger, failure patterns, promotion check.

    python -m src.signals_display.learning_runner --journal J --status-file F [--candidate-report R]

Opens the journal read-only and writes only the status file (atomic, private).
Run it off the live server (for example on a separate machine or at low priority).
The live server only reads the status file. Nothing here changes the live model.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from . import learning_loop as ll


def _candidate_metrics(path):
    """Fresh-market metrics from an offline shadow-evaluation report, or None."""
    if not path:
        return None, None
    report = json.loads(Path(path).read_text())
    fresh = report.get("fresh") or {}
    if not fresh.get("contracts"):
        return None, None
    cur = fresh.get("existing_every_market") or {}
    cand = fresh.get("combined_every_market") or {}
    to_block = lambda m: {"correct": m.get("correct"), "calls": m.get("calls"), "markets": m.get("markets"),
                          "log_loss": m.get("log_loss")}
    return to_block(cand), to_block(cur)


def run(journal, status_file, asof, candidate_report=None):
    with closing(sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        ledger = ll.build_ledger(db, asof)
    patterns = ll.failure_patterns(ledger)
    candidate, current = _candidate_metrics(candidate_report)
    if candidate is None:
        promotion = {"decision": "HOLD", "reasons": ["no candidate report with fresh markets was supplied"],
                     "applied": False, "fresh_markets": 0,
                     "rollback": "the current live model is unchanged"}
    else:
        promotion = ll.promotion_decision(candidate, current)
        promotion["candidate"], promotion["current"] = candidate, current
    status = ll.summarize(ledger, patterns, promotion, asof.isoformat())
    ll.write_status(status_file, status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True,
                        help="use <journal>.learning-status.json, the path the live dashboard reads")
    parser.add_argument("--candidate-report", type=Path)
    parser.add_argument("--asof")
    args = parser.parse_args(argv)
    asof = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    status = run(args.journal, args.status_file, asof, args.candidate_report)
    print(json.dumps({k: status[k] for k in ("status", "ledger", "promotion")}, indent=1, default=str))


if __name__ == "__main__":
    main()

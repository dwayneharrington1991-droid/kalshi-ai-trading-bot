#!/usr/bin/env python3
"""Print calibration and performance for settled shadow outcomes in SQLite."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import aiosqlite

from src.agents.calibration import calibration_report
from src.backtesting.metrics import summarize


async def report(db_path: str, minimum_samples: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("""
            SELECT predicted_probability_yes, actual_outcome
            FROM prediction_outcomes WHERE outcome_status = 'settled'
            ORDER BY prediction_timestamp, id
        """)).fetchall()
    if not rows:
        return {
            "mode": "SHADOW_SIMULATION", "profitability_evidence": False,
            "status": "INSUFFICIENT_DATA", "sample_count": 0,
        }
    observations = [(float(row[0]), int(row[1])) for row in rows]
    metrics = summarize([
        {"probability_yes": probability, "outcome": outcome, "action": "ABSTAIN", "pnl": 0}
        for probability, outcome in observations
    ])
    buckets = [bucket.__dict__ for bucket in calibration_report(observations, minimum_samples=minimum_samples)]
    return {
        "mode": "SHADOW_SIMULATION", "profitability_evidence": False,
        "status": "COMPLETE", "metrics": metrics, "calibration": buckets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--minimum-samples", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(report(args.database, args.minimum_samples)), sort_keys=True, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

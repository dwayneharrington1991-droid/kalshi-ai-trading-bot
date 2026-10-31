#!/usr/bin/env python3
"""Run one deterministic offline replay from local historical JSON."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.backtesting.costs import CostModel
from src.backtesting.replay import ReplayEngine, ReplaySnapshot
from src.backtesting.report import json_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    args = parser.parse_args()
    payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    snapshots = [ReplaySnapshot(**row) for row in payload]
    result = ReplayEngine(
        CostModel(fee_rate=args.fee_rate, slippage=args.slippage), seed=args.seed
    ).run(snapshots)
    print(json_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


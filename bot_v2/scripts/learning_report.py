from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from src.learning.confidence_optimizer import ConfidenceOptimizer
from src.learning.pattern_memory import PatternMemory
from src.learning.performance_tracker import PerformanceTracker
from src.learning.strategy_ranker import StrategyRanker


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a read-only learning report."
    )
    parser.add_argument(
        "--database",
        default="data/trade_memory.db",
        help="Path to the trade-memory SQLite database.",
    )
    parser.add_argument(
        "--minimum-samples",
        type=int,
        default=20,
        help="Minimum sample size for optimizer recommendations.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the report as JSON.",
    )
    args = parser.parse_args()

    tracker = PerformanceTracker(args.database)

    report = {
        "overall": asdict(tracker.overall_summary()),
        "strategies": StrategyRanker(
            args.database,
            minimum_sample_size=args.minimum_samples,
        ).rank(),
        "confidence_buckets": ConfidenceOptimizer(
            args.database,
            minimum_samples=args.minimum_samples,
        ).analyze(),
        "patterns": PatternMemory(args.database).analyze(
            minimum_samples=args.minimum_samples
        ),
    }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return

    print("\n=== LEARNING REPORT ===")
    print("\nOVERALL")
    for key, value in report["overall"].items():
        print(f"{key}: {value}")

    print("\nSTRATEGIES")
    if not report["strategies"]:
        print("No completed strategy data yet.")
    else:
        for item in report["strategies"]:
            print(item)

    print("\nCONFIDENCE BUCKETS")
    if not report["confidence_buckets"]:
        print("No completed confidence data yet.")
    else:
        for item in report["confidence_buckets"]:
            print(item)

    print("\nREPEATING PATTERNS")
    if not report["patterns"]:
        print("No pattern has enough samples yet.")
    else:
        for item in report["patterns"]:
            print(item)


if __name__ == "__main__":
    main()

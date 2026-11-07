"""Read-only audit of immutable opening forecasts against official outcomes."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from .model import timestamp
from .opening_models import dataset_coverage
from .prediction_engine import recorded_opening_direction
from .scores import score, wilson_interval


def _number(features, name):
    value = features.get(name)
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _conditions(features):
    r1, r5, r15 = (_number(features, name) for name in ("return_1m", "return_5m", "return_15m"))
    volatility = _number(features, "volatility_change")
    expansion = _number(features, "candle_atr_zscore_20")
    volume = _number(features, "relative_volume")
    breakout = _number(features, "breakout_distance")
    result = []
    if r5 is not None and r15 is not None and abs(r5) < 0.001 and abs(r15) < 0.002:
        result.append("SIDEWAYS_LOW_MOMENTUM")
    if (volatility is not None and volatility > 1.5) or (expansion is not None and expansion > 1.5):
        result.append("VOLATILITY_SPIKE")
    if r1 is not None and r15 is not None and r1 * r15 < 0:
        result.append("SHORT_LONG_TREND_REVERSAL")
    if volume is not None and volume < 0.75:
        result.append("LOW_RELATIVE_VOLUME")
    if breakout is not None and r1 is not None and breakout * r1 < 0:
        result.append("FALSE_BREAKOUT_CANDIDATE")
    return result or ["OTHER_OR_UNCLASSIFIED"]


def audit(db, asof=None):
    asof = asof or datetime.now(timezone.utc)
    rows = db.execute(
        """SELECT b.ticker,b.generated_at,b.cutoff,b.closes_at,b.up,b.payload,
          o.observed_at,o.outcome FROM btc_engine_opening b
          JOIN forecast_outcomes o ON o.ticker=b.ticker AND o.origin='official'
          WHERE b.mode='PUBLIC MARKET DATA' AND b.up IS NOT NULL
            AND julianday(o.observed_at)<=julianday(?)
          ORDER BY b.generated_at,b.ticker""",
        (asof.isoformat(),),
    ).fetchall()
    duplicate_openings = db.execute(
        """SELECT COUNT(*) FROM (SELECT ticker,COUNT(*) n FROM btc_engine_opening
        WHERE mode='PUBLIC MARKET DATA' GROUP BY ticker HAVING n>1)"""
    ).fetchone()[0]
    duplicate_outcomes = db.execute(
        """SELECT COUNT(*) FROM (SELECT ticker,COUNT(*) n FROM forecast_outcomes
        WHERE origin='official' GROUP BY ticker HAVING n>1)"""
    ).fetchone()[0]
    calls, probabilities, outcomes = [], [], []
    invalid = Counter()
    failure_conditions = Counter()
    direction_counts = Counter()
    for raw in rows:
        try:
            opening = json.loads(raw[5])
            generated, cutoff, closed, observed = map(timestamp, (raw[1], raw[2], raw[3], raw[6]))
            opened = timestamp(opening["opens_at"])
            if not opened <= generated < closed <= observed <= asof:
                raise ValueError("chronology")
            if cutoff != opened or timestamp(opening["data_cutoff"]) != cutoff:
                raise ValueError("cutoff")
            frozen = opening["frozen_features"]
            if timestamp(frozen["max_input_received_at"]) > cutoff:
                raise ValueError("future_input")
            p = float(raw[4])
            if not math.isfinite(p) or not 0 <= p <= 1 or raw[7] not in (0, 1):
                raise ValueError("probability_or_outcome")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid[str(exc)] += 1
            continue
        direction = recorded_opening_direction(opening)
        direction_counts[direction] += 1
        probabilities.append(p)
        outcomes.append(int(raw[7]))
        if direction in {"UP", "DOWN"}:
            correct = (direction == "UP") == bool(raw[7])
            calls.append(correct)
            if not correct:
                failure_conditions.update(_conditions(frozen.get("values") or {}))
    correct = sum(calls)
    directional = len(calls)
    coverage = dataset_coverage(db, asof)
    return {
        "generated_at": asof.isoformat(),
        "read_only": True,
        "scored_probability_records": len(probabilities),
        "directional_calls": directional,
        "correct": correct,
        "wrong": directional - correct,
        "wait_or_neutral": len(probabilities) - directional,
        "directional_coverage": directional / len(probabilities) if probabilities else None,
        "directional_accuracy": correct / directional if directional else None,
        "directional_accuracy_interval_95": wilson_interval(correct, directional),
        "probability_metrics": score(probabilities, outcomes),
        "directions": dict(sorted(direction_counts.items())),
        "incorrect_call_conditions": dict(failure_conditions.most_common()),
        "invalid_or_unscored": dict(sorted(invalid.items())),
        "duplicate_opening_tickers": duplicate_openings,
        "duplicate_official_outcome_tickers": duplicate_outcomes,
        "training_eligibility": coverage,
        "integrity": {
            "future_inputs_accepted": 0,
            "chronology_checked": True,
            "saved_wait_decisions_preserved": True,
            "historical_records_modified": False,
        },
        "note": "Failure categories are descriptive fixed rules, not fitted causal claims. No model is promoted by this audit.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    args = parser.parse_args()
    uri = args.journal.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute("PRAGMA query_only=ON")
        print(json.dumps(audit(db), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

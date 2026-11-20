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


def _confidence_band(probability):
    confidence = max(probability, 1 - probability)
    bands = (
        (0.50, 0.55),
        (0.55, 0.60),
        (0.60, 0.65),
        (0.65, 0.70),
        (0.70, 0.75),
        (0.75, 0.80),
        (0.80, 0.90),
        (0.90, 1.01),
    )
    for lower, upper in bands:
        if confidence < upper:
            return f"{lower:.0%}-{min(upper, 1):.0%}"
    raise AssertionError("unreachable confidence band")


def _group_metrics(records, key):
    result = {}
    for name in sorted({str(record[key]) for record in records}):
        subset = [record for record in records if str(record[key]) == name]
        directional = [record for record in subset if record["direction"] in {"UP", "DOWN"}]
        correct = sum(record["correct"] is True for record in directional)
        result[name] = {
            "records": len(subset),
            "directional_calls": len(directional),
            "wait": len(subset) - len(directional),
            "correct": correct,
            "wrong": len(directional) - correct,
            "directional_accuracy": correct / len(directional) if directional else None,
            "directional_accuracy_interval_95": wilson_interval(correct, len(directional)),
            "coverage": len(directional) / len(subset) if subset else None,
            "probability_metrics": score(
                [record["probability_up"] for record in subset],
                [record["official_outcome"] for record in subset],
            ),
        }
    return result


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
    baseline_probabilities, baseline_outcomes = [], []
    records = []
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
            max_input_received = timestamp(frozen["max_input_received_at"])
            input_cutoff = timestamp(opening["input_data_cutoff"])
            if max_input_received >= cutoff or input_cutoff >= cutoff:
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
        baseline = opening.get("baseline_probability_up")
        if type(baseline) in (int, float) and math.isfinite(baseline) and 0 <= baseline <= 1:
            baseline_probabilities.append(float(baseline))
            baseline_outcomes.append(int(raw[7]))
        values = frozen.get("values") or {}
        active = opening.get("feature_usage", {}).get("active_probability_inputs") or []
        available = [name for name, value in values.items() if value is not None]
        correct = None
        if direction in {"UP", "DOWN"}:
            correct = (direction == "UP") == bool(raw[7])
            calls.append(correct)
            if not correct:
                failure_conditions.update(_conditions(values))
        records.append(
            {
                "ticker": raw[0],
                "opens_at": opened.isoformat(),
                "generated_at": generated.isoformat(),
                "opening_delay_seconds": (generated - opened).total_seconds(),
                "data_cutoff": cutoff.isoformat(),
                "input_data_cutoff": input_cutoff.isoformat(),
                "max_input_received_at": max_input_received.isoformat(),
                "closes_at": closed.isoformat(),
                "official_outcome_observed_at": observed.isoformat(),
                "model": opening.get("model", "UNRECORDED"),
                "model_selection_status": (opening.get("model_selection") or {}).get("status"),
                "direction": direction,
                "probability_up": p,
                "confidence_band": _confidence_band(p),
                "confidence": opening.get("confidence"),
                "estimated_edge": opening.get("estimated_edge"),
                "threshold": opening.get("threshold"),
                "reference_price": opening.get("reference_price"),
                "reference_source": opening.get("reference_source"),
                "historical_candle_status": (opening.get("data_quality") or {}).get("status"),
                "closed_history_bars": (opening.get("data_quality") or {}).get(
                    "closed_history_bars"
                ),
                "frozen_feature_version": frozen.get("version"),
                "available_feature_count": len(available),
                "active_probability_inputs": active,
                "available_shadow_feature_count": len(
                    opening.get("feature_usage", {}).get("available_shadow_features") or []
                ),
                "official_outcome": int(raw[7]),
                "correct": correct,
                "scoring_rule": "UP is correct iff official outcome=1; DOWN iff outcome=0; WAIT is not directional",
                "preopening_inputs_verified": True,
                "failure_conditions": _conditions(values) if correct is False else [],
            }
        )
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
        "saved_statistical_baseline_metrics": score(
            baseline_probabilities, baseline_outcomes
        ),
        "directions": dict(sorted(direction_counts.items())),
        "by_model": _group_metrics(records, "model"),
        "by_confidence_band": _group_metrics(records, "confidence_band"),
        "records": records,
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

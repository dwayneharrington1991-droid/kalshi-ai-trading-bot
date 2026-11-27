"""Read-only baseline measurement of the live opening model, on a snapshot copy of the journal.

Writes nothing. Reports only aggregates: accuracy among recorded UP/DOWN calls, coverage, calibration,
log loss, Brier score, regime breakdowns and feature redundancy. Feature redundancy uses only frozen
point-in-time feature values already stored with each opening.

    python -m src.signals_display.opening_baseline --snapshot SNAP.db --asof 2026-11-19T20:00:00+00:00
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from . import model_lifecycle as lc
from . import opening_research as research


def _brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def _log_loss(pairs):
    eps = 1e-12
    return (-sum(y * math.log(min(max(p, eps), 1 - eps)) + (1 - y) * math.log(min(max(1 - p, eps), 1 - eps))
                 for p, y in pairs) / len(pairs)) if pairs else None


def _call_correct(direction, outcome):
    return (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)


def summarize(rows):
    total = len(rows)
    calls = [r for r in rows if r.get("recorded_direction") in ("UP", "DOWN")]
    correct = sum(1 for r in calls if _call_correct(r["recorded_direction"], int(r["outcome"])))
    probs = [(float(r["recorded_up"]), int(r["outcome"])) for r in rows if r.get("recorded_up") is not None]
    regimes = {}
    for r in rows:
        key = str(r.get("regime") or "unknown").split("_")[0]
        bucket = regimes.setdefault(key, {"openings": 0, "calls": 0, "correct": 0})
        bucket["openings"] += 1
        if r.get("recorded_direction") in ("UP", "DOWN"):
            bucket["calls"] += 1
            bucket["correct"] += 1 if _call_correct(r["recorded_direction"], int(r["outcome"])) else 0
    for bucket in regimes.values():
        bucket["accuracy"] = round(bucket["correct"] / bucket["calls"], 4) if bucket["calls"] else None
    return {
        "eligible_openings": total,
        "directional_calls": len(calls),
        "coverage": round(len(calls) / total, 4) if total else None,
        "verified_accuracy_on_calls": round(correct / len(calls), 4) if calls else None,
        "scored_probabilities": len(probs),
        "log_loss": round(_log_loss(probs), 5) if probs else None,
        "brier": round(_brier(probs), 5) if probs else None,
        "calibration_ece": round(lc._ece(probs), 5) if probs else None,
        "regimes": regimes,
    }


def redundancy(rows, top=10, collinear_threshold=0.98):
    """Feature health on the stored frozen values: constant features, strongly collinear pairs, outcome correlation."""
    names = sorted({k for r in rows for k in r["features"]})
    cols = {n: [float(r["features"].get(n, 0.0)) for r in rows] for n in names}
    n = len(rows)

    def mean(xs):
        return sum(xs) / len(xs)

    def corr(a, b):
        ma, mb = mean(a), mean(b)
        da = [x - ma for x in a]
        db = [y - mb for y in b]
        den = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
        return sum(x * y for x, y in zip(da, db)) / den if den > 0 else 0.0

    constant = [name for name in names if max(cols[name]) == min(cols[name])]
    live = [name for name in names if name not in set(constant)]
    outcome = [float(r["outcome"]) for r in rows]
    outcome_corr = sorted(((abs(corr(cols[name], outcome)), name) for name in live), reverse=True)[:top]
    collinear = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            c = corr(cols[a], cols[b])
            if abs(c) >= collinear_threshold:
                collinear.append((round(abs(c), 4), a, b))
    return {
        "feature_names_seen": len(names),
        "constant_features": len(constant),
        "non_constant_features": len(live),
        "strongly_collinear_pairs": len(collinear),
        "top_outcome_correlations": [{"feature": name, "abs_corr": round(c, 4)} for c, name in outcome_corr],
        "example_collinear_pairs": sorted(collinear, reverse=True)[:top],
        "openings_used": n,
    }


def run(snapshot, asof):
    with sqlite3.connect(f"file:{Path(snapshot).resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        rows = research.load(db, asof)
    return {
        "asof": asof.isoformat(),
        "performance": summarize(rows),
        "redundancy": redundancy(rows) if rows else None,
        "writes": "none",
    }


def _main(argv=None):
    ap = argparse.ArgumentParser(description="read-only opening-model baseline")
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--asof", required=True)
    args = ap.parse_args(argv)
    asof = datetime.fromisoformat(args.asof.replace("Z", "+00:00"))
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    print(json.dumps(run(args.snapshot, asof), indent=1, sort_keys=True, default=str))


if __name__ == "__main__":
    _main()

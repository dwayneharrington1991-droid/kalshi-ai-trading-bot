"""Research-only matched comparison of frozen price and order-flow features.

The input is a JSONL export containing one immutable decision snapshot per
sample.  This module never opens a production journal, writes a model artifact,
or changes inference.  It deliberately requires a separate, pre-written JSON
specification so feature names, the evaluation interval, the fixed decision
threshold and regularisation cannot be selected after outcomes are inspected.

Example record (all timestamps must be timezone-aware)::

    {"sample_id":"KXBTC15M-...", "target":"opening_final",
     "decision_at":"2026-11-25T12:00:00Z",
     "label_observed_at":"2026-11-25T12:16:00Z", "label":"UP",
     "baseline_probability":0.52,
     "baseline_generated_at":"2026-11-25T11:59:59Z",
     "families":{
       "price":{"received_at":"2026-11-25T11:59:59Z",
                "event_at":"2026-11-25T11:59:58.9Z",
                "values":{"return_60s":0.001}},
       "flow":{"received_at":"2026-11-25T11:59:59Z",
               "values":{"cvd_60s":1.2}}}}

The spec fixes ``target``, ``eval_start``, ``eval_end``, ``min_train``,
``threshold``, ``l2`` and ``candidates``.  Candidate features use dotted names,
for example ``price.return_60s``.  Every reported model and the recorded
baseline are evaluated on the same intersection of eligible samples.  The
expanding fit for a decision may use only labels whose receipt precedes that
decision.  Preprocessing is fitted inside each chronological training fold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np


ROLLING_CALLS = (25, 50, 100, 300)
DEVELOPMENT_END = datetime.fromisoformat("2026-11-20T06:00:00+00:00")
TARGETS = {"opening_final": (0, 0, 900, "official_kalshi"),
           "first6": (0, 0, 360, "verified_btc_reference"),
           "minute6_final": (360, 367, 900, "official_kalshi")}


def _ts(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _probability(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"invalid {field}")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"invalid {field}")
    return value


def _label(value):
    if type(value) not in (int, str):
        raise ValueError("label must be UP/DOWN or 1/0")
    if value in ("UP", 1):
        return 1
    if value in ("DOWN", 0):
        return 0
    raise ValueError("label must be UP/DOWN or 1/0")


def _load_spec(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ("target", "eval_start", "min_train", "threshold", "l2", "candidates")
    if any(key not in raw for key in required):
        raise ValueError("spec is missing required fields")
    raw["eval_start"] = _ts(raw["eval_start"], "eval_start")
    raw["eval_end"] = _ts(raw["eval_end"], "eval_end") if raw.get("eval_end") else None
    # Prospective mode (a pre-registered forward test): every record must be decided at or after the registration
    # time, which must precede eval_start; nothing from the protected development/holdout periods is allowed.
    raw["prospective_start"] = _ts(raw["prospective_start"], "prospective_start") if raw.get("prospective_start") else None
    if raw["prospective_start"] is not None:
        if raw["prospective_start"] < DEVELOPMENT_END or raw["eval_start"] < raw["prospective_start"]:
            raise ValueError("prospective_start must follow the development period and precede eval_start")
        if raw["eval_end"] is not None and raw["eval_end"] <= raw["eval_start"]:
            raise ValueError("eval_end must follow eval_start")
    elif raw["eval_end"] is None or not raw["eval_start"] < raw["eval_end"] <= DEVELOPMENT_END:
        raise ValueError("eval_end must follow eval_start and stay within the documented development period")
    if not isinstance(raw["min_train"], int) or raw["min_train"] < 20:
        raise ValueError("min_train must be an integer >= 20")
    raw["threshold"] = _probability(raw["threshold"], "threshold")
    if not 0.5 < raw["threshold"] < 1:
        raise ValueError("threshold must be between 0.5 and 1")
    if isinstance(raw["l2"], bool) or not isinstance(raw["l2"], (int, float)) \
            or not math.isfinite(raw["l2"]) or raw["l2"] <= 0:
        raise ValueError("l2 must be finite and positive")
    if not isinstance(raw["target"], str) or not raw["target"]:
        raise ValueError("target must be a nonempty string")
    if (not isinstance(raw["candidates"], dict) or "price_only" not in raw["candidates"]
            or "recorded_baseline" in raw["candidates"]):
        raise ValueError("candidates must include price_only")
    for name, features in raw["candidates"].items():
        if not isinstance(name, str) or not name or not isinstance(features, list) or not features:
            raise ValueError("every candidate needs a nonempty feature list")
        if len(features) != len(set(features)) or any(not isinstance(x, str) or "." not in x for x in features):
            raise ValueError("candidate features must be unique dotted names")
    if raw.get("competition"):
        if raw["target"] not in TARGETS or raw["threshold"] != .55:
            raise ValueError("competition requires a supported target and the fixed 0.55 threshold")
        candidates = raw["candidates"]
        if set(candidates) != {"price_only", "price_walls", "price_walls_trades"}:
            raise ValueError("competition requires all three nested candidates")
        a, b, c = (set(candidates[name]) for name in ("price_only", "price_walls", "price_walls_trades"))
        if (not a < b < c or not all(x.startswith("price.") for x in a)
                or not all(x.startswith("walls.") for x in b - a)
                or not all(x.startswith("trades.") for x in c - b)):
            raise ValueError("competition requires price, then wall, then executed-trade additions")
        age = raw.get("flow_max_age_s")
        if type(age) not in (int, float) or not math.isfinite(age) or age <= 0:
            raise ValueError("competition requires a positive flow freshness limit")
    return raw


def load_records(path, spec):
    """Load and point-in-time validate immutable snapshots.

    Malformed rows fail the entire run.  Silently dropping a losing or
    inconvenient row would make selective accuracy impossible to audit.
    """
    records, seen, markets = [], set(), set()
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                sample_id = raw["sample_id"]
                if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
                    raise ValueError("missing or duplicate sample_id")
                if raw.get("target") != spec["target"]:
                    raise ValueError("target does not match fixed spec")
                decision = _ts(raw["decision_at"], "decision_at")
                if spec.get("prospective_start") is not None:
                    if decision < spec["prospective_start"]:
                        raise ValueError("record precedes the pre-registered prospective start")
                elif decision >= DEVELOPMENT_END:
                    raise ValueError("record outside the documented development period")
                label_at = _ts(raw["label_observed_at"], "label_observed_at")
                baseline_at = _ts(raw["baseline_generated_at"], "baseline_generated_at")
                if label_at <= decision:
                    raise ValueError("label was available at or before the decision")
                if baseline_at > decision:
                    raise ValueError("baseline was generated after the decision")
                metadata = {}
                if spec.get("competition"):
                    opened = _ts(raw["opens_at"], "opens_at")
                    market = raw["market_id"]
                    earliest, latest, outcome_delay, origin = TARGETS[spec["target"]]
                    if (not isinstance(market, str) or not market or market in markets
                            or opened.timestamp() % 900 != 0):
                        raise ValueError("duplicate market or invalid 15-minute opening")
                    if not opened + timedelta(seconds=earliest) <= decision <= opened + timedelta(seconds=latest):
                        raise ValueError("decision outside target cutoff")
                    if label_at < opened + timedelta(seconds=outcome_delay) or raw.get("label_origin") != origin:
                        raise ValueError("outcome lacks target-specific timing or verified source")
                    if not opened + timedelta(seconds=earliest) <= baseline_at <= decision:
                        raise ValueError("baseline generated outside target decision window")
                    version, direction = raw.get("baseline_model_version"), raw.get("baseline_call")
                    if not isinstance(version, str) or not version or direction not in ("UP", "DOWN", "WAIT"):
                        raise ValueError("competition requires the recorded baseline call and model version")
                    metadata = {"market_id": market, "baseline_call": direction, "baseline_model_version": version,
                                "regime": raw.get("volatility_regime") if isinstance(raw.get("volatility_regime"), str)
                                else "unknown"}
                families = raw.get("families")
                if not isinstance(families, dict):
                    raise ValueError("families must be an object")
                flat = {}
                for family_name, family in families.items():
                    if not isinstance(family_name, str) or not family_name or "." in family_name or not isinstance(family, dict):
                        raise ValueError("invalid feature family")
                    received = _ts(family["received_at"], f"{family_name}.received_at")
                    if received > decision:
                        raise ValueError("feature family was received after the decision")
                    if family.get("event_at") is not None:
                        event = _ts(family["event_at"], f"{family_name}.event_at")
                        if event > received:
                            raise ValueError("feature event time is after receipt time")
                    if spec.get("competition") and family_name in ("walls", "trades"):
                        if (family.get("quality") != "VERIFIED" or family.get("event_at") is None
                                or (decision - event).total_seconds() > spec["flow_max_age_s"]):
                            continue
                    values = family.get("values")
                    if not isinstance(values, dict):
                        raise ValueError("feature values must be an object")
                    for feature_name, value in values.items():
                        if not feature_name or "." in feature_name:
                            raise ValueError("invalid feature name")
                        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                                or not math.isfinite(value):
                            raise ValueError("features must be finite numbers")
                        flat[f"{family_name}.{feature_name}"] = float(value)
                records.append({
                    "sample_id": sample_id,
                    "decision_at": decision,
                    "label_at": label_at,
                    "y": _label(raw["label"]),
                    "baseline": _probability(raw["baseline_probability"], "baseline_probability"),
                    "features": flat,
                    **metadata,
                })
                seen.add(sample_id)
                if metadata:
                    markets.add(metadata["market_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid record at line {line_number}: {exc}") from exc
    records.sort(key=lambda row: (row["decision_at"], row["sample_id"]))
    if any(records[i]["decision_at"] == records[i - 1]["decision_at"]
           and records[i]["sample_id"] == records[i - 1]["sample_id"] for i in range(1, len(records))):
        raise ValueError("duplicate chronological record")
    return records


def _fit(X, y, l2, iterations=60):
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std < 1e-12] = 1.0
    normalized = (X - mean) / std
    design = np.column_stack([np.ones(len(normalized)), normalized])
    weights = np.zeros(design.shape[1])
    penalty = np.full(design.shape[1], float(l2))
    penalty[0] = 0.0
    for _ in range(iterations):
        p = 1 / (1 + np.exp(-np.clip(design @ weights, -30, 30)))
        gradient = design.T @ (p - y) + penalty * weights
        curvature = (design * (p * (1 - p))[:, None]).T @ design
        hessian = curvature + np.diag(penalty) + np.eye(len(weights)) * 1e-9
        step = np.linalg.solve(hessian, gradient)
        weights -= step
        if float(np.max(np.abs(step))) < 1e-8:
            break
    return mean, std, weights


def _predict(model, row):
    mean, std, weights = model
    x = (np.asarray(row, dtype=float) - mean) / std
    score = weights[0] + x @ weights[1:]
    return float(1 / (1 + np.exp(-np.clip(score, -30, 30))))


def walk_forward(records, spec):
    """Return matched expanding-walk-forward probabilities.

    A row is eligible only when every feature used by every fixed candidate is
    present.  This makes all model and baseline comparisons paired on exactly
    the same decisions.  Training labels must have arrived strictly before the
    evaluation decision.
    """
    required = sorted({feature for features in spec["candidates"].values() for feature in features})
    development = [row for row in records if row["decision_at"] < spec["eval_start"]]
    evaluation = [row for row in records if row["decision_at"] >= spec["eval_start"]
                  and (spec["eval_end"] is None or row["decision_at"] < spec["eval_end"])]
    eligible = [row for row in evaluation if all(name in row["features"] for name in required)]
    predictions = []
    for test in eligible:
        train = [row for row in records
                 if row["decision_at"] < test["decision_at"] and row["label_at"] < test["decision_at"]
                 and all(name in row["features"] for name in required)]
        if len(train) < spec["min_train"]:
            continue
        result = {"sample_id": test["sample_id"], "decision_at": test["decision_at"],
                  "y": test["y"], "baseline": test["baseline"], "train_n": len(train), "p": {}}
        result.update({key: test[key] for key in ("baseline_call", "baseline_model_version", "regime") if key in test})
        for name, names in spec["candidates"].items():
            X = np.asarray([[row["features"][feature] for feature in names] for row in train], dtype=float)
            y = np.asarray([row["y"] for row in train], dtype=float)
            model = _fit(X, y, spec["l2"])
            result["p"][name] = _predict(model, [test["features"][feature] for feature in names])
        predictions.append(result)
    return predictions, {
        "records": len(records), "development_records": len(development),
        "evaluation_records": len(evaluation), "matched_feature_records": len(eligible),
        "scored_records": len(predictions), "required_features": required,
    }


def _wilson(correct, total, z=1.96):
    if not total:
        return [None, None]
    p, denominator = correct / total, 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [center - radius, center + radius]


def _metrics(probabilities, labels, threshold, recorded_calls=None):
    p, y = np.asarray(probabilities, dtype=float), np.asarray(labels, dtype=int)
    if recorded_calls is None:
        calls = (p >= threshold) | (p <= round(1 - threshold, 15))
        directions = p >= threshold
    else:
        calls = np.asarray([call != "WAIT" for call in recorded_calls], dtype=bool)
        directions = np.asarray([call == "UP" for call in recorded_calls], dtype=bool)
    correct = int(np.sum((directions == y) & calls))
    total_calls = int(np.sum(calls))
    eps = 1e-9
    clipped = np.clip(p, eps, 1 - eps)
    bins = []
    for index in range(10):
        low, high = index / 10, (index + 1) / 10
        use = (p >= low) & (p < high if index < 9 else p <= 1)
        if np.any(use):
            bins.append({"low": round(float(low), 1), "n": int(np.sum(use)),
                         "mean_probability": float(np.mean(p[use])), "up_rate": float(np.mean(y[use]))})
    rolling = {}
    call_results = ((directions == y)[calls]).tolist()
    for window in ROLLING_CALLS:
        values = call_results[-window:]
        rolling[str(window)] = {"n": len(values), "accuracy": sum(values) / len(values) if values else None}
    return {
        "n": len(y), "correct": correct, "wrong": total_calls - correct,
        "wrong_up": int(np.sum(calls & directions & (y == 0))),
        "wrong_down": int(np.sum(calls & ~directions & (y == 1))),
        "wait": len(y) - total_calls, "coverage": total_calls / len(y) if len(y) else None,
        "accuracy": correct / total_calls if total_calls else None,
        "accuracy_ci95": _wilson(correct, total_calls),
        "brier": float(np.mean((p - y) ** 2)) if len(y) else None,
        "log_loss": float(-np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))) if len(y) else None,
        "calibration_bins": bins, "rolling_call_accuracy": rolling,
    }


def _paired_ci(candidate, baseline, labels, seed=5, repetitions=3000):
    y = np.asarray(labels, dtype=float)
    differences = (np.asarray(candidate) - y) ** 2 - (np.asarray(baseline) - y) ** 2
    if not len(differences):
        return [None, None]
    rng = random.Random(seed)
    means = sorted(sum(differences[rng.randrange(len(differences))] for _ in differences) / len(differences)
                   for _ in range(repetitions))
    return [float(means[int(0.025 * repetitions)]), float(means[int(0.975 * repetitions)])]


def evaluate(records_path, spec_path):
    spec = _load_spec(spec_path)
    records = load_records(records_path, spec)
    predictions, coverage = walk_forward(records, spec)
    if not predictions:
        return {"status": "INSUFFICIENT_DATA", "coverage": coverage,
                "reason": "No matched evaluation row had the preregistered minimum past labels"}
    labels = [row["y"] for row in predictions]
    series = {"recorded_baseline": [row["baseline"] for row in predictions]}
    for name in spec["candidates"]:
        series[name] = [row["p"][name] for row in predictions]
    if spec.get("competition"):
        members = ("price_only", "price_walls", "price_walls_trades")
        series["combined_research"] = [sum(row["p"][name] for name in members) / len(members)
                                       for row in predictions]
    recorded_calls = [row["baseline_call"] for row in predictions] if spec.get("competition") else None
    metrics = {name: _metrics(values, labels, spec["threshold"],
                              recorded_calls if name == "recorded_baseline" else None)
               for name, values in series.items()}
    price = series["price_only"]
    comparisons = {}
    for name, values in series.items():
        if name == "price_only":
            continue
        delta = metrics[name]["brier"] - metrics["price_only"]["brier"]
        comparisons[name] = {"brier_minus_price_only": delta,
                             "paired_bootstrap_ci95": _paired_ci(values, price, labels)}
    spec_bytes = Path(spec_path).read_bytes()
    competition = {}
    if spec.get("competition"):
        days = [row["decision_at"].astimezone(timezone.utc).date().isoformat() for row in predictions]
        regimes = [row["regime"] for row in predictions]
        competition = {"scoreboard_schema": "order-flow-competition-v1", "approaches": {},
                       "last_decision_at": predictions[-1]["decision_at"].isoformat()}
        for name, values in series.items():
            calls = recorded_calls if name == "recorded_baseline" else None
            groups = {}
            partitions_by_field = [("day_utc", days), ("volatility_regime", regimes)]
            if calls is not None:
                partitions_by_field.append(("model_version", [row["baseline_model_version"] for row in predictions]))
            for field, partitions in partitions_by_field:
                groups[field] = {}
                for group in sorted(set(partitions)):
                    idx = [i for i, item in enumerate(partitions) if item == group]
                    groups[field][group] = _metrics([values[i] for i in idx], [labels[i] for i in idx],
                                                   spec["threshold"], [calls[i] for i in idx] if calls else None)
            versions = (sorted({row["baseline_model_version"] for row in predictions}) if calls
                        else ["equal-weight-abc-v1"] if name == "combined_research"
                        else ["regularized-logistic-v1"])
            competition["approaches"][name] = {
                **metrics[name], "versions": versions, "groups": groups,
                "missing_from_evaluation": coverage["evaluation_records"] - len(predictions),
                "coverage_all_evaluation": (metrics[name]["correct"] + metrics[name]["wrong"])
                / coverage["evaluation_records"],
                "calibration_status": "DESCRIPTIVE_OUT_OF_SAMPLE",
                "status": "RECORDED_BASELINE" if calls else "RESEARCH_ONLY",
            }
        comparisons["trades_increment"] = {
            "brier_minus_price_walls": metrics["price_walls_trades"]["brier"] - metrics["price_walls"]["brier"],
            "paired_bootstrap_ci95": _paired_ci(series["price_walls_trades"], series["price_walls"], labels),
        }
    with Path(records_path).open("rb") as records_file:
        records_hash = hashlib.file_digest(records_file, "sha256").hexdigest()
    return {
        "status": "RESEARCH_ONLY", "target": spec["target"],
        "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "records_sha256": records_hash,
        "method": "expanding chronological fit; label receipt strictly before decision; fold-local scaling; matched samples",
        "threshold": spec["threshold"], "coverage": coverage, "metrics": metrics,
        "comparisons": comparisons,
        **competition,
        "warnings": [
            "No result changes production or authorizes model promotion.",
            "Use only development data outside protected V3/V4/V5 holdouts.",
            "A displayed wall is resting liquidity; it is not an executed trade or proof of spoofing.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    output = Path(args.out)
    if output.suffix != ".json" or output.resolve() in {Path(args.records).resolve(), Path(args.spec).resolve()}:
        parser.error("--out must be a separate .json report")
    if output.exists() or output.is_symlink():
        parser.error("--out must be a new path")
    report = evaluate(args.records, args.spec)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str, allow_nan=False)
        handle.write("\n")
    return 0 if report["status"] == "RESEARCH_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

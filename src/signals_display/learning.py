"""Purged chronological model comparison using official labels only.

No trading/network. JSON models only. No Kalshi odds in the feature vocabulary.
Insufficient official labels produce a report, never invented fitted weights.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3

from .features import FEATURES
from .model import timestamp
from .scores import score, horizon

MIN_CONTRACTS = 170


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-35, min(35, z))))


def logit(p):
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def dataset(db):
    rows = db.execute(
        "SELECT payload,settlement_up,settled_at FROM predictions WHERE settlement_origin='official' AND settlement_up IS NOT NULL ORDER BY timestamp,id"
    ).fetchall()
    selected = {}
    for raw in rows:
        state = json.loads(raw[0])
        info = state.get("model", {})
        market = state.get("market") or {}
        f = info.get("candidate_features", {}).get("values", {})
        if state.get("stale") or f.get("distance_scaled") is None or f.get("return_1m") is None:
            continue
        t = timestamp(state["now"])
        close = timestamp(market["closes_at"])
        if t >= close or not raw[2] or timestamp(raw[2]) < close:
            continue
        baseline = info.get("baseline_probability_up")
        if baseline is None:
            continue
        q = market.get("quotes", {}).get("up", {})
        mid = (
            (q["bid"] + q["ask"]) / 2
            if q.get("bid") is not None and q.get("ask") is not None
            else 0.5
        )
        key = (market["ticker"], horizon((close - t).total_seconds()))
        selected.setdefault(
            key,
            dict(
                ticker=market["ticker"],
                timestamp=t.isoformat(),
                closes_at=close.isoformat(),
                label_available_at=raw[2],
                features=f,
                outcome=int(raw[1]),
                baseline=baseline,
                market=mid,
                remaining=(close - t).total_seconds(),
            ),
        )
    return list(selected.values())


def preprocess(rows):
    names = []
    means = []
    scales = []
    for name in FEATURES:
        values = [r["features"].get(name) for r in rows if r["features"].get(name) is not None]
        values = [float(v) for v in values if math.isfinite(float(v))]
        if len(values) < len(rows) / 2:
            continue
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        if var < 1e-12:
            continue
        names.append(name)
        means.append(mean)
        scales.append(math.sqrt(var))
    return {"names": names, "means": means, "scales": scales}


def transform(features, prep):
    vector = []
    missing = []
    for name, mean, scale in zip(prep["names"], prep["means"], prep["scales"]):
        value = features.get(name)
        absent = value is None or not isinstance(value, (int, float)) or not math.isfinite(value)
        vector.append(0 if absent else max(-8, min(8, (value - mean) / scale)))
        missing.append(1 if absent else 0)
    return vector + missing + [1.0]


def fit(rows, kind, l2, prep):
    xs = [transform(r["features"], prep) for r in rows]
    ys = [r["outcome"] for r in rows]
    w = [0.0] * len(xs[0])
    w[-1] = logit(sum(ys) / len(ys)) if kind == "logistic" else sum(ys) / len(ys)
    for _ in range(220):
        grad = [0.0] * len(w)
        for x, y in zip(xs, ys):
            z = sum(a * b for a, b in zip(w, x))
            pred = sigmoid(z) if kind == "logistic" else z
            for j, value in enumerate(x):
                grad[j] += (pred - y) * value / len(xs)
        for j in range(len(w)):
            w[j] -= 0.06 * (grad[j] + (l2 * w[j] / len(xs) if j < len(w) - 1 else 0))
    return {"kind": kind, "weights": w, "preprocessor": prep, "l2": l2, "calibration": [1.0, 0.0]}


def predict(model, features):
    x = transform(features, model["preprocessor"])
    z = sum(a * b for a, b in zip(model["weights"], x))
    p = sigmoid(z) if model["kind"] == "logistic" else max(1e-6, min(1 - 1e-6, z))
    a, b = model.get("calibration", [1.0, 0.0])
    return sigmoid(a * logit(p) + b)


def calibrate(model, rows):
    z = [logit(predict(model, r["features"])) for r in rows]
    ys = [r["outcome"] for r in rows]
    a, b = 1.0, 0.0
    for _ in range(150):
        errors = [sigmoid(a * t + b) - y for t, y in zip(z, ys)]
        a -= 0.02 * sum(e * t for e, t in zip(errors, z)) / len(rows)
        b -= 0.02 * sum(errors) / len(rows)
    model["calibration"] = [a, b]


def compare(rows):
    contracts = sorted(
        {r["ticker"] for r in rows},
        key=lambda t: min(timestamp(r["closes_at"]) for r in rows if r["ticker"] == t),
    )
    report = {
        "status": "INSUFFICIENT_OFFICIAL_LABELS",
        "official_contracts": len(contracts),
        "required_contracts": MIN_CONTRACTS,
        "trained": False,
        "edge_status": "edge unverified",
        "method": "Grouped by contract; chronological train/validation/untouched test; overlapping label windows and late labels purged. Preprocessing on train only, calibration/selection on validation only.",
    }
    if len(contracts) < MIN_CONTRACTS:
        return None, report
    n = len(contracts)
    a = int(n * 0.6)
    b = int(n * 0.8)
    sets = [set(contracts[:a]), set(contracts[a:b]), set(contracts[b:])]
    groups = [[r for r in rows if r["ticker"] in s] for s in sets]
    val_start = min(timestamp(r["timestamp"]) for r in groups[1])
    test_start = min(timestamp(r["timestamp"]) for r in groups[2])
    groups[0] = [
        r
        for r in groups[0]
        if timestamp(r["closes_at"]) < val_start and timestamp(r["label_available_at"]) <= val_start
    ]
    groups[1] = [
        r
        for r in groups[1]
        if timestamp(r["closes_at"]) < test_start
        and timestamp(r["label_available_at"]) <= test_start
    ]
    train, val, test = groups
    counts = [len({r["ticker"] for r in g}) for g in groups]
    report["split_contracts"] = counts
    if any(c < m for c, m in zip(counts, [100, 30, 30])):
        report["status"] = "INSUFFICIENT_PURGED_SPLITS"
        return None, report
    if (
        min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10
        or min(sum(r["outcome"] == y for r in val) for y in (0, 1)) < 5
    ):
        report["status"] = "INSUFFICIENT_CLASS_BALANCE"
        return None, report
    prep = preprocess(train)
    if not prep["names"]:
        report["status"] = "NO_USABLE_TRAINING_FEATURES"
        return None, report
    prior = (sum(r["outcome"] for r in train) + 1) / (len(train) + 2)
    ys = [r["outcome"] for r in val]
    base_val = score([r["baseline"] for r in val], ys)
    prior_val = score([prior] * len(val), ys)
    candidates = []
    for kind in ("logistic", "ridge"):
        for l2 in (0.1, 1.0):
            model = fit(train, kind, l2, prep)
            calibrate(model, val)
            result = score([predict(model, r["features"]) for r in val], ys)
            candidates.append((result["log_loss"], model, result))
    _, chosen, validation = min(candidates, key=lambda item: item[0])
    beats_validation = all(
        validation[k] < min(base_val[k], prior_val[k]) for k in ("brier", "log_loss")
    )
    ablations = {}
    for group, names in {
        "cross_venue": {"cross_venue_divergence", "spot_depth_imbalance"},
        "derivatives": {"derivatives_basis", "open_interest_change"},
        "macro_news": {"public_macro_release_count"},
        "candles": set(FEATURES[2:13]),
    }.items():
        keep = [i for i, name in enumerate(prep["names"]) if name not in names]
        if len(keep) == len(prep["names"]):
            ablations[group] = {"status": "UNAVAILABLE_IN_TRAINING"}
            continue
        reduced = {k: [prep[k][i] for i in keep] for k in ("names", "means", "scales")}
        candidate = fit(train, chosen["kind"], chosen["l2"], reduced)
        calibrate(candidate, val)
        metrics = score([predict(candidate, r["features"]) for r in val], ys)
        ablations[group] = {
            "without_group": metrics,
            "validation_log_loss_improvement_with_group": metrics["log_loss"]
            - validation["log_loss"],
            "note": "Validation diagnostic only; not causal evidence or a profitability claim",
        }
    # Selection is frozen before unsealing the untouched test labels.
    tested = {
        "selected_candidate": score(
            [predict(chosen, r["features"]) for r in test], [r["outcome"] for r in test]
        ),
        "zero_drift": score([r["baseline"] for r in test], [r["outcome"] for r in test]),
        "market_implied": score([r["market"] for r in test], [r["outcome"] for r in test]),
        "train_prior": score([prior] * len(test), [r["outcome"] for r in test]),
    }
    passed = (
        beats_validation
        and tested["selected_candidate"]["brier"] < tested["zero_drift"]["brier"]
        and tested["selected_candidate"]["log_loss"] < tested["zero_drift"]["log_loss"]
        and all(
            tested["selected_candidate"][k] < tested["train_prior"][k]
            for k in ("brier", "log_loss")
        )
    )
    by_horizon = {
        h: score(
            [predict(chosen, r["features"]) for r in test if horizon(r["remaining"]) == h],
            [r["outcome"] for r in test if horizon(r["remaining"]) == h],
        )
        for h in (">10m", "5-10m", "2-5m", "<=2m")
    }
    report.update(
        status="EXPERIMENTAL_CANDIDATE" if passed else "BASELINE_RETAINED",
        trained=True,
        selected_before_test=chosen["kind"],
        validation=validation,
        validation_baseline=base_val,
        validation_train_prior=prior_val,
        validation_input_ablations=ablations,
        candidate_validation=[
            {"kind": m["kind"], "l2": m["l2"], "metrics": v} for _, m, v in candidates
        ],
        test=tested,
        test_by_horizon=by_horizon,
        feature_names=prep["names"],
        train_end=max(r["label_available_at"] for r in train),
        validation_end=max(r["label_available_at"] for r in val),
        test_first_prediction=min(r["timestamp"] for r in test),
        note="Final holdout is a predeclared deployment gate, never used to retune coefficients. No profitability or 90% accuracy claim. No repeat tuning against this test period.",
    )
    if not passed:
        return None, report
    cutoff = max(r["label_available_at"] for r in rows)
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    artifact = {
        "format": "kxbtc-independent-v1",
        "model": chosen,
        "model_id": fingerprint[:12],
        "labels_available_through": cutoff,
        "report": report,
        "validated": False,
    }
    return artifact, report


def load_artifact(path):
    if path is None or not Path(path).is_file():
        return (
            None,
            "No trained artifact; collecting official labels and retaining experimental baseline",
        )
    try:
        data = json.loads(Path(path).read_text())
        m = data["model"]
        prep = m["preprocessor"]
        if data["format"] != "kxbtc-independent-v1" or m["kind"] not in ("logistic", "ridge"):
            raise ValueError("format")
        if any(name not in FEATURES for name in prep["names"]):
            raise ValueError("non-independent or unknown features")
        if not len(prep["names"]) == len(prep["means"]) == len(prep["scales"]):
            raise ValueError("preprocessor shape")
        if len(m["weights"]) != 2 * len(prep["names"]) + 1:
            raise ValueError("weight shape")
        if not all(
            math.isfinite(v)
            for v in m["weights"] + prep["means"] + prep["scales"] + m["calibration"]
        ):
            raise ValueError("nonfinite weights")
        if any(v <= 0 for v in prep["scales"]):
            raise ValueError("scale")
        if len(m["calibration"]) != 2 or len(set(prep["names"])) != len(prep["names"]):
            raise ValueError("calibration or duplicate feature shape")
        report = data["report"]
        if report.get("status") != "EXPERIMENTAL_CANDIDATE" or not report.get("trained"):
            raise ValueError("candidate did not pass chronological deployment gate")
        if (
            any(c < minimum for c, minimum in zip(report["split_contracts"], [100, 30, 30]))
            or len(report["split_contracts"]) != 3
        ):
            raise ValueError("insufficient purged sample counts")
        timestamp(data["labels_available_through"])
        return (
            data,
            "Validation-selected candidate loaded; experimental, not a proven profitable edge",
        )
    except (KeyError, ValueError, TypeError) as exc:
        return None, "Invalid artifact rejected: " + str(exc)


def main():
    parser = argparse.ArgumentParser(
        description="Offline independent forecast candidate comparison; official signal-journal labels only"
    )
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--output", type=Path, default=Path("signals_model.json"))
    args = parser.parse_args()
    # A read-only connection cannot create or modify the user's journal.
    db = sqlite3.connect("file:" + str(args.journal.resolve()) + "?mode=ro", uri=True)
    try:
        rows = dataset(db)
    finally:
        db.close()
    artifact, report = compare(rows)
    args.output.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n")
    if artifact:
        args.output.write_text(json.dumps(artifact, indent=2) + "\n")
    print(
        report["status"]
        + ": "
        + str(report["official_contracts"])
        + " official contracts; edge unverified"
    )
    if not artifact:
        print(
            "No candidate artifact written; keep the simple baseline. Existing artifact is not overwritten."
        )


if __name__ == "__main__":
    main()

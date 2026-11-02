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
PROTOCOL = "purged-linear-ensemble-validation-grid-v2"


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-35, min(35, z))))


def logit(p):
    p = max(1e-6, min(1 - 1e-6, p))
    return math.log(p / (1 - p))


def dataset(db, asof=None):
    asof = asof or datetime.now(timezone.utc)
    has_results = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_outcomes'"
    ).fetchone()
    result_filter = (
        "AND EXISTS (SELECT 1 FROM forecast_outcomes o WHERE o.ticker=predictions.ticker "
        "AND o.origin='official' AND o.outcome=predictions.settlement_up "
        "AND julianday(o.observed_at)<=julianday(?)) "
        if has_results
        else ""
    )
    # Project only small scalar feature data inside SQLite. Cursor iteration
    # never materializes gigabytes of historical JSON payloads in Python.
    rows = db.execute(
        """SELECT ticker,timestamp,settlement_up,settled_at,
        json_extract(payload,'$.stale','$.market.closes_at',
          '$.model.candidate_features.values','$.model.baseline_probability_up',
          '$.market.quotes.up.bid','$.market.quotes.up.ask')
        FROM predictions WHERE mode='PUBLIC MARKET DATA'
          AND settlement_origin='official' AND settlement_up IS NOT NULL
          AND julianday(settled_at)<=julianday(?) """
        + result_filter
        + "ORDER BY timestamp,id",
        (asof.isoformat(),) * (2 if has_results else 1),
    )
    selected = {}
    for raw in rows:
        stale, closes_at, f, baseline, bid, ask = json.loads(raw[4])
        if (
            stale
            or not isinstance(f, dict)
            or f.get("distance_scaled") is None
            or f.get("return_1m") is None
        ):
            continue
        t = timestamp(raw[1])
        close = timestamp(closes_at)
        if t >= close or not raw[3] or not close <= timestamp(raw[3]) <= asof:
            continue
        if (
            type(baseline) not in (int, float)
            or not math.isfinite(baseline)
            or not 0 <= baseline <= 1
        ):
            continue
        mid = (
            (bid + ask) / 2
            if all(type(v) in (int, float) and math.isfinite(v) for v in (bid, ask))
            and 0 <= bid <= ask <= 1
            else None
        )
        key = (raw[0], horizon((close - t).total_seconds()))
        selected.setdefault(
            key,
            dict(
                ticker=raw[0],
                timestamp=t.isoformat(),
                closes_at=close.isoformat(),
                label_available_at=raw[3],
                features=f,
                outcome=int(raw[2]),
                baseline=baseline,
                market=mid,
                remaining=(close - t).total_seconds(),
            ),
        )
    return list(selected.values())


def official_label_count(db, asof):
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_outcomes'"
    ).fetchone()
    if exists:
        return db.execute(
            "SELECT COUNT(*) FROM forecast_outcomes WHERE origin='official' AND julianday(observed_at)<=julianday(?)",
            (asof.isoformat(),),
        ).fetchone()[0]
    # Compatibility for old journals without the additive official-result table.
    return db.execute(
        "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE settlement_origin='official' "
        "AND settlement_up IS NOT NULL AND julianday(settled_at)<=julianday(?)",
        (asof.isoformat(),),
    ).fetchone()[0]


def market_score(rows):
    supported = [r for r in rows if r.get("market") is not None]
    return score([r["market"] for r in supported], [r["outcome"] for r in supported])


def prepare_comparison(db, now, *, used_test_contracts=None):
    count = official_label_count(db, now)
    if count < MIN_CONTRACTS:
        artifact, report = compare([])
        report.update(
            official_contracts=count,
            count_basis="Recorded official settlement results; featured eligible training contracts can only be fewer",
            dataset_loaded=False,
            reason="Minimum independent event count not met; historical payloads were not read or fitted",
        )
        return artifact, report, []
    rows = dataset(db, now)
    artifact, report = compare(rows, used_test_contracts=used_test_contracts)
    report["dataset_loaded"] = True
    return artifact, report, rows


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


def predict(model, features, *, baseline_probability=None):
    if model["kind"] == "volatility_baseline":
        if (
            isinstance(baseline_probability, bool)
            or not isinstance(baseline_probability, (int, float))
            or not math.isfinite(baseline_probability)
            or not 0 <= baseline_probability <= 1
        ):
            raise ValueError("Ensemble requires the independent event-aware baseline probability")
        return float(baseline_probability)
    if model["kind"] == "ensemble":
        return sum(
            weight * predict(component, features, baseline_probability=baseline_probability)
            for weight, component in zip(model["mixture_weights"], model["components"])
            if weight
        )
    x = transform(features, model["preprocessor"])
    z = sum(a * b for a, b in zip(model["weights"], x))
    p = sigmoid(z) if model["kind"] == "logistic" else max(1e-6, min(1 - 1e-6, z))
    a, b = model.get("calibration", [1.0, 0.0])
    return sigmoid(a * logit(p) + b)


def row_predict(model, row):
    return predict(model, row["features"], baseline_probability=row["baseline"])


def ensemble_candidate(components, validation):
    """Convex weights selected on past validation only; zero weights are allowed.

    The event-aware volatility model is supplied separately from features. Kalshi
    market prices are never an input to this independent ensemble.
    """
    predictions = [[row_predict(m, r) for r in validation] for m in components]
    outcomes = [r["outcome"] for r in validation]
    best = None
    for a in range(11):
        for b in range(11 - a):
            weights = [a / 10, b / 10, (10 - a - b) / 10]
            probabilities = [
                sum(w * p[i] for w, p in zip(weights, predictions)) for i in range(len(validation))
            ]
            metrics = score(probabilities, outcomes)
            candidate = (metrics["log_loss"], metrics["brier"], weights, metrics)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
    model = {
        "kind": "ensemble",
        "components": components,
        "mixture_weights": best[2],
        "weight_method": "nonnegative simplex grid, step 0.1; validation log loss then Brier; frozen before test",
        "requires_independent_baseline": True,
    }
    return model, best[3]


def candidates(train, validation, prep):
    result = []
    for kind in ("logistic", "ridge"):
        for penalty in (0.1, 1.0):
            model = fit(train, kind, penalty, prep)
            calibrate(model, validation)
            metrics = score(
                [row_predict(model, r) for r in validation], [r["outcome"] for r in validation]
            )
            result.append((metrics["log_loss"], model, metrics))
    members = [
        min((r for r in result if r[1]["kind"] == kind), key=lambda r: r[0])[1]
        for kind in ("logistic", "ridge")
    ]
    combined, metrics = ensemble_candidate(members + [{"kind": "volatility_baseline"}], validation)
    result.append((metrics["log_loss"], combined, metrics))
    return result


def uncertainty(probabilities, rows):
    """Descriptive uncertainty uses contracts as clusters, never independent ticks."""
    import random
    from .live_model import wilson

    grouped = {}
    for p, r in zip(probabilities, rows):
        grouped.setdefault(r["ticker"], []).append((p, r["outcome"]))
    ordered = list(grouped.values())
    losses = [score([p for p, _ in group], [y for _, y in group]) for group in ordered]
    first_correct = sum((group[0][0] >= 0.5) == bool(group[0][1]) for group in ordered)
    result = {
        "contracts": len(grouped),
        "first_forecast_accuracy_interval": wilson(first_correct, len(grouped))
        if grouped
        else None,
        "brier_interval": None,
        "log_loss_interval": None,
        "note": "Descriptive 95% uncertainty: first forecast per contract Wilson accuracy interval; contiguous-five-contract block bootstrap of contract-mean losses. Nonstationarity can invalidate intervals; not evidence of profitability.",
    }
    if len(losses) < 10:
        return result
    rng = random.Random(3150)
    samples = {"brier": [], "log_loss": []}
    n = len(losses)
    for _ in range(200):
        draw = []
        while len(draw) < n:
            start = rng.randrange(n)
            draw.extend(losses[(start + i) % n] for i in range(min(5, n - len(draw))))
        for name in samples:
            samples[name].append(sum(row[name] for row in draw) / n)
    for name, values in samples.items():
        values.sort()
        result[name + "_interval"] = [values[4], values[194]]
    return result


def calibrate(model, rows):
    z = [logit(predict(model, r["features"])) for r in rows]
    ys = [r["outcome"] for r in rows]
    a, b = 1.0, 0.0
    for _ in range(150):
        errors = [sigmoid(a * t + b) - y for t, y in zip(z, ys)]
        a -= 0.02 * sum(e * t for e, t in zip(errors, z)) / len(rows)
        b -= 0.02 * sum(errors) / len(rows)
    model["calibration"] = [a, b]


def compare(rows, *, used_test_contracts=None):
    contracts = sorted(
        {r["ticker"] for r in rows},
        key=lambda t: min(timestamp(r["closes_at"]) for r in rows if r["ticker"] == t),
    )
    report = {
        "status": "INSUFFICIENT_OFFICIAL_LABELS",
        "official_contracts": len(contracts),
        "required_contracts": MIN_CONTRACTS,
        "trained": False,
        "protocol": PROTOCOL,
        "edge_status": "edge unverified",
        "candidate_catalog": {
            "logistic": "Implemented, train-only standardized independent features",
            "ridge": "Implemented linear probability comparison",
            "ensemble": "Implemented validation-weighted logistic/ridge/event-aware volatility baseline; no market odds inputs",
            "random_forest_gradient_boosting": "Deferred: current minimum 170 contracts is already small for the linear candidate search; require a new predeclared protocol and larger independent labeled sample before adding tree-family search",
            "sequence_models": "Deferred: insufficient independent event labels; no deep-learning advantage assumed",
        },
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
    if used_test_contracts is not None:
        overlap = {r["ticker"] for r in test} & set(used_test_contracts)
        if overlap:
            report.update(
                status="HOLDOUT_REUSE_BLOCKED",
                holdout_overlap_contracts=len(overlap),
                holdout_reuse_checked=True,
            )
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
    pool = candidates(train, val, prep)
    # Stable ordering favors a simpler individual model when scores tie.
    _, chosen, validation = min(pool, key=lambda item: item[0])
    beats_validation = all(
        validation[k] < min(base_val[k], prior_val[k]) for k in ("brier", "log_loss")
    )
    ablations = {}
    for group, names in {
        "advanced_technicals": {
            name
            for name in FEATURES
            if name.startswith("advanced_")
            and name
            not in {
                "advanced_vwap_deviation_fraction",
                "advanced_flow_imbalance",
                "advanced_cvd_slope",
                "advanced_book_imbalance",
                "advanced_effective_spread",
                "advanced_trade_intensity",
                "advanced_profile_threshold_concentration",
            }
        },
        "executed_trade_microstructure": {
            "advanced_vwap_deviation_fraction",
            "advanced_flow_imbalance",
            "advanced_cvd_slope",
            "advanced_book_imbalance",
            "advanced_effective_spread",
            "advanced_trade_intensity",
            "advanced_profile_threshold_concentration",
        },
        "microstructure_horizons": {name for name in FEATURES if name.startswith("micro_")},
        "cross_venue": {"cross_venue_divergence", "spot_depth_imbalance"},
        "derivatives": {"derivatives_basis", "open_interest_change"},
        "macro_news": {"public_macro_release_count"},
        "order_flow": {
            "spot_cvd_delta_fraction",
            "perpetual_cvd_delta_fraction",
            "spot_book_dominance",
        },
        "edge_reading": {
            "spot_absorption_candidate",
            "perpetual_absorption_candidate",
            "spot_delta_efficiency_3m",
            "perpetual_delta_efficiency_3m",
        },
        "history_24h": {
            "return_24h",
            "rsi_14",
            "macd_scaled",
            "ema_gap",
            "vwap_deviation",
            "bollinger_z",
            "volatility_24h",
        },
        "adaptive_momentum": {
            "adaptive_momentum_consistency",
            "adaptive_momentum_strength",
            "adaptive_threshold_distance",
            "adaptive_acceleration",
            "adaptive_reversal",
        },
        "candles": set(FEATURES[2:13]),
    }.items():
        keep = [i for i, name in enumerate(prep["names"]) if name not in names]
        if len(keep) == len(prep["names"]):
            ablations[group] = {"status": "UNAVAILABLE_IN_TRAINING"}
            continue
        reduced = {k: [prep[k][i] for i in keep] for k in ("names", "means", "scales")}
        if chosen["kind"] == "ensemble":
            candidate = next(
                m for _, m, _ in candidates(train, val, reduced) if m["kind"] == "ensemble"
            )
        else:
            candidate = fit(train, chosen["kind"], chosen["l2"], reduced)
            calibrate(candidate, val)
        metrics = score([row_predict(candidate, r) for r in val], ys)
        ablations[group] = {
            "without_group": metrics,
            "validation_log_loss_improvement_with_group": metrics["log_loss"]
            - validation["log_loss"],
            "note": "Validation diagnostic only; not causal evidence or a profitability claim",
        }
    # Selection is frozen before unsealing the untouched test labels.
    tested = {
        "selected_candidate": score(
            [row_predict(chosen, r) for r in test], [r["outcome"] for r in test]
        ),
        "zero_drift": score([r["baseline"] for r in test], [r["outcome"] for r in test]),
        "market_implied": market_score(test),
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
            [row_predict(chosen, r) for r in test if horizon(r["remaining"]) == h],
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
            {
                "kind": m["kind"],
                "l2": m.get("l2"),
                "mixture_weights": m.get("mixture_weights"),
                "metrics": v,
            }
            for _, m, v in pool
        ],
        test=tested,
        test_by_horizon=by_horizon,
        feature_names=prep["names"],
        train_end=max(r["label_available_at"] for r in train),
        validation_end=max(r["label_available_at"] for r in val),
        test_first_prediction=min(r["timestamp"] for r in test),
        test_contracts=sorted({r["ticker"] for r in test}),
        test_fingerprint=hashlib.sha256(json.dumps(test, sort_keys=True).encode()).hexdigest(),
        holdout_reuse_checked=used_test_contracts is not None,
        test_uncertainty=uncertainty([row_predict(chosen, r) for r in test], test),
        ensemble_weights=chosen.get("mixture_weights"),
        expected_calibration_error=sum(
            b["samples"]
            / tested["selected_candidate"]["samples"]
            * abs(b["mean_probability"] - b["outcome_rate"])
            for b in tested["selected_candidate"]["calibration"]
            if b["samples"]
        ),
        note="Final holdout is a predeclared deployment gate, never used to retune coefficients. No profitability or 90% accuracy claim. No repeat tuning against this test period.",
    )
    if not passed:
        return None, report
    cutoff = max(r["label_available_at"] for r in rows)
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    artifact = {
        "format": "kxbtc-independent-v2"
        if chosen["kind"] == "ensemble"
        else "kxbtc-independent-v1",
        "model": chosen,
        "model_id": fingerprint[:12],
        "labels_available_through": cutoff,
        "report": report,
        "validated": False,
        "protocol": PROTOCOL,
    }
    return artifact, report


def validate_model(model, *, component=False):
    if not isinstance(model, dict):
        raise ValueError("model must be an object")
    if model.get("kind") == "volatility_baseline":
        if not component or set(model) != {"kind"}:
            raise ValueError("baseline is only an explicitly supplied ensemble component")
        return
    if model.get("kind") == "ensemble":
        members, weights = model.get("components"), model.get("mixture_weights")
        if (
            component
            or not isinstance(members, list)
            or not isinstance(weights, list)
            or len(members) != 3
            or len(weights) != 3
        ):
            raise ValueError("ensemble shape; nested ensembles are not supported")
        if not model.get("requires_independent_baseline"):
            raise ValueError("independent baseline input requirement missing")
        if {m.get("kind") for m in members if isinstance(m, dict)} != {
            "logistic",
            "ridge",
            "volatility_baseline",
        }:
            raise ValueError("unsupported ensemble family")
        if (
            any(
                type(w) not in (int, float) or not math.isfinite(w) or not 0 <= w <= 1
                for w in weights
            )
            or abs(sum(weights) - 1) > 1e-9
        ):
            raise ValueError("ensemble weights must form a nonnegative unit simplex")
        for member in members:
            validate_model(member, component=True)
        return
    if model.get("kind") not in ("logistic", "ridge"):
        raise ValueError("unknown model family")
    prep = model["preprocessor"]
    if not all(isinstance(prep.get(k), list) for k in ("names", "means", "scales")):
        raise ValueError("preprocessor arrays")
    if (
        not prep["names"]
        or len(prep["names"]) > len(FEATURES)
        or any(name not in FEATURES for name in prep["names"])
    ):
        raise ValueError("non-independent or unknown features")
    if not len(prep["names"]) == len(prep["means"]) == len(prep["scales"]):
        raise ValueError("preprocessor shape")
    if (
        not isinstance(model["weights"], list)
        or len(model["weights"]) != 2 * len(prep["names"]) + 1
    ):
        raise ValueError("weight shape")
    if (
        not isinstance(model["calibration"], list)
        or len(model["calibration"]) != 2
        or len(set(prep["names"])) != len(prep["names"])
    ):
        raise ValueError("calibration or duplicate feature shape")
    if any(
        type(v) not in (int, float) or not math.isfinite(v)
        for v in model["weights"] + prep["means"] + prep["scales"] + model["calibration"]
    ):
        raise ValueError("nonfinite or invalid weights")
    if any(v <= 0 for v in prep["scales"]):
        raise ValueError("scale")


def validate_artifact(data):
    if not isinstance(data, dict) or data.get("format") not in (
        "kxbtc-independent-v1",
        "kxbtc-independent-v2",
    ):
        raise ValueError("format")
    validate_model(data["model"])
    if data["model"]["kind"] == "ensemble" and data["format"] != "kxbtc-independent-v2":
        raise ValueError("ensemble requires versioned v2 format")
    report = data["report"]
    if report.get("status") != "EXPERIMENTAL_CANDIDATE" or not report.get("trained"):
        raise ValueError("candidate did not pass chronological deployment gate")
    counts = report["split_contracts"]
    if (
        not isinstance(counts, list)
        or len(counts) != 3
        or any(type(c) is not int or c < minimum for c, minimum in zip(counts, [100, 30, 30]))
    ):
        raise ValueError("insufficient purged sample counts")
    timestamp(data["labels_available_through"])
    if (
        not isinstance(data.get("model_id"), str)
        or not data["model_id"]
        or len(data["model_id"]) > 128
    ):
        raise ValueError("model identity")
    return data


def load_artifact(path):
    if path is None or not Path(path).is_file():
        return (
            None,
            "No trained artifact; collecting official labels and retaining experimental baseline",
        )
    try:
        if Path(path).stat().st_size > 2_000_000:
            raise ValueError("model JSON exceeds bounded size")
        data = validate_artifact(json.loads(Path(path).read_text()))
        return (
            data,
            "Validation-selected candidate loaded; experimental, not a proven profitable edge",
        )
    except (KeyError, ValueError, TypeError, AttributeError, OSError, RecursionError) as exc:
        return None, "Invalid artifact rejected: " + str(exc)


def main():
    from contextlib import nullcontext
    from .model_registry import ModelRegistry, atomic_json

    parser = argparse.ArgumentParser(
        description="Offline independent forecast candidate comparison; official signal-journal labels only"
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Also write a separate expanding-window validation report",
    )
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--output", type=Path, default=Path("signals_model.json"))
    parser.add_argument(
        "--registry", type=Path, help="Archive immutable JSON versions and record exposed holdouts"
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Respect registry update interval, archive only, never activate",
    )
    parser.add_argument("--minimum-update-hours", type=int, default=24)
    args = parser.parse_args()
    if args.scheduled and (args.registry is None or args.walk_forward):
        parser.error(
            "--scheduled requires --registry and cannot unseal additional --walk-forward test blocks"
        )
    registry = ModelRegistry(args.registry) if args.registry else None
    try:
        with registry.update_lock() if registry else nullcontext():
            now = datetime.now(timezone.utc)
            if args.scheduled and not registry.due(now, args.minimum_update_hours):
                print("MODEL_UPDATE_NOT_DUE: active artifact unchanged")
                return
            # A read-only connection cannot create or modify the user's journal.
            db = sqlite3.connect("file:" + str(args.journal.resolve()) + "?mode=ro", uri=True)
            try:
                artifact, report, rows = prepare_comparison(
                    db,
                    now,
                    used_test_contracts=registry.used_test_contracts() if registry else None,
                )
            finally:
                db.close()
            if args.walk_forward:
                atomic_json(
                    args.output.with_suffix(".walkforward.json"), walk_forward_compare(rows)
                )
            atomic_json(args.output.with_suffix(".report.json"), report)
            if registry:
                registry.record_evaluation(report, now)
            if artifact:
                if registry:
                    version = registry.register(artifact, now)
                    print("Archived experimental model version " + version)
                if args.scheduled:
                    print(
                        "Scheduled comparison cannot activate a model; use model_registry --activate after review"
                    )
                else:
                    atomic_json(args.output, artifact)
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
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))


def walk_forward_compare(rows, development_contracts=170, block_contracts=30):
    """Expanding past-only train/validation, disjoint future evaluation blocks."""
    contracts = sorted(
        {r["ticker"] for r in rows},
        key=lambda t: min(timestamp(r["closes_at"]) for r in rows if r["ticker"] == t),
    )
    report = {
        "status": "INSUFFICIENT_OFFICIAL_LABELS",
        "folds": [],
        "note": "Expanding chronological development; disjoint future contract blocks. Training-only preprocessing, validation-only calibration/selection; labels unavailable at forecast time purged. No profitability claim.",
    }
    for index in range(
        development_contracts, len(contracts) - block_contracts + 1, block_contracts
    ):
        past = set(contracts[:index])
        future = set(contracts[index : index + block_contracts])
        split = int(index * 0.8)
        train_ids = set(contracts[:split])
        val_ids = past - train_ids
        test = [r for r in rows if r["ticker"] in future]
        test_start = min(timestamp(r["timestamp"]) for r in test)
        val = [
            r
            for r in rows
            if r["ticker"] in val_ids
            and timestamp(r["label_available_at"]) <= test_start
            and timestamp(r["closes_at"]) < test_start
        ]
        if not val:
            continue
        val_start = min(timestamp(r["timestamp"]) for r in val)
        train = [
            r
            for r in rows
            if r["ticker"] in train_ids
            and timestamp(r["label_available_at"]) <= val_start
            and timestamp(r["closes_at"]) < val_start
        ]
        if len({r["ticker"] for r in train}) < 100 or len({r["ticker"] for r in val}) < 30:
            continue
        if (
            min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10
            or min(sum(r["outcome"] == y for r in val) for y in (0, 1)) < 5
        ):
            continue
        prep = preprocess(train)
        if not prep["names"]:
            continue
        pool = candidates(train, val, prep)
        _, chosen, validation = min(pool, key=lambda r: r[0])
        baseline = score([r["baseline"] for r in val], [r["outcome"] for r in val])
        use_candidate = all(validation[k] < baseline[k] for k in ("brier", "log_loss"))
        scored = score(
            [row_predict(chosen, r) if use_candidate else r["baseline"] for r in test],
            [r["outcome"] for r in test],
        )
        report["folds"].append(
            {
                "train_contracts": len({r["ticker"] for r in train}),
                "validation_contracts": len({r["ticker"] for r in val}),
                "test_contracts": sorted(future),
                "test_first_prediction": test_start.isoformat(),
                "train_last_label": max(r["label_available_at"] for r in train),
                "validation_last_label": max(r["label_available_at"] for r in val),
                "selected": chosen["kind"] if use_candidate else "zero_drift",
                "ensemble_weights": chosen.get("mixture_weights") if use_candidate else None,
                "validation": validation,
                "test": scored,
                "market_implied": market_score(test),
                "zero_drift": score([r["baseline"] for r in test], [r["outcome"] for r in test]),
            }
        )
    if report["folds"]:
        report["status"] = "EXPERIMENTAL_WALK_FORWARD"
    return report


if __name__ == "__main__":
    main()

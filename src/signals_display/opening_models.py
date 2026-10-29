"""Opening-cutoff-only shadow learning. No network, account access or promotion.

Live-horizon artifacts and post-opening observations are never borrowed to
reconstruct a missing opening forecast. This module writes reports and optional
shadow artifacts only; it has no activation path.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import sqlite3

from . import learning
from .features import FEATURES
from .model import timestamp
from .scores import score

MIN_CONTRACTS = learning.MIN_CONTRACTS
BOOST_MIN_CONTRACTS = 500
PROTOCOL = "opening-frozen-linear-ensemble-stumps-shadow-v1"
FEATURE_VERSION = "opening-asof-features-v2"


def _opening_rows(db, asof):
    return db.execute(
        """SELECT o.ticker,b.generated_at,b.cutoff,b.closes_at,b.up,b.payload,
          o.observed_at,o.outcome FROM forecast_outcomes o
          LEFT JOIN btc_engine_opening b ON b.ticker=o.ticker
            AND b.mode='PUBLIC MARKET DATA'
          WHERE o.origin='official' AND julianday(o.observed_at)<=julianday(?)
          ORDER BY o.observed_at,o.ticker""",
        (asof.isoformat(),),
    )


def _assess_opening(raw, asof):
    """Return an eligible row or one explicit, non-overlapping exclusion reason."""
    if raw[5] is None:
        return None, "NO_FROZEN_OPENING_RECORD"
    if raw[4] is None or not isinstance(raw[4], (int, float)) or not 0 <= raw[4] <= 1:
        return None, "OPENING_PROBABILITY_UNAVAILABLE"
    try:
        opening = json.loads(raw[5])
        frozen = opening["frozen_features"]
        opened = timestamp(opening["opens_at"])
        closed = timestamp(raw[3])
        generated = timestamp(raw[1])
        cutoff = timestamp(raw[2])
        observed = timestamp(raw[6])
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, json.JSONDecodeError):
        return None, "INVALID_OR_INCOMPLETE_FROZEN_PAYLOAD"
    if frozen.get("version") != FEATURE_VERSION:
        return None, "INCOMPATIBLE_FEATURE_VERSION"
    if raw[7] not in (0, 1):
        return None, "INVALID_OFFICIAL_OUTCOME"
    if not opened <= generated < closed <= observed <= asof:
        return None, "INVALID_PREDICTION_LABEL_CHRONOLOGY"
    try:
        if cutoff != opened or timestamp(frozen.get("cutoff")) != cutoff:
            return None, "CUTOFF_DOES_NOT_EQUAL_MARKET_OPEN"
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None, "INVALID_OR_INCOMPLETE_FROZEN_PAYLOAD"
    try:
        if timestamp(frozen["max_input_received_at"]) > cutoff:
            return None, "POST_OPENING_INPUT_DETECTED"
        if timestamp(opening["data_cutoff"]) != cutoff:
            return None, "PAYLOAD_CUTOFF_MISMATCH"
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None, "INVALID_OR_INCOMPLETE_FROZEN_PAYLOAD"
    if (
        opening.get("up") != raw[4]
        or opening.get("generated_at") != raw[1]
        or opening.get("closes_at") != raw[3]
    ):
        return None, "DATABASE_PAYLOAD_MISMATCH"
    if not 60 <= (closed - opened).total_seconds() <= 900:
        return None, "UNSUPPORTED_CONTRACT_DURATION"
    if not _source_receipts_valid(frozen.get("sources", {}), cutoff):
        return None, "SOURCE_RECEIPT_AFTER_OPEN"
    try:
        features = {
            name: float(value)
            for name, value in frozen["values"].items()
            if name in FEATURES and type(value) in (int, float) and math.isfinite(value)
        }
    except (TypeError, KeyError, AttributeError):
        return None, "INVALID_OR_INCOMPLETE_FROZEN_PAYLOAD"
    if features.get("distance_scaled") is None or features.get("return_1m") is None:
        return None, "REQUIRED_OPENING_FEATURES_MISSING"
    try:
        baseline = float(opening.get("baseline_probability_up", raw[4]))
    except (ValueError, TypeError):
        return None, "INVALID_BASELINE_PROBABILITY"
    if not math.isfinite(baseline) or not 0 <= baseline <= 1:
        return None, "INVALID_BASELINE_PROBABILITY"
    return dict(
        ticker=raw[0], timestamp=generated.isoformat(), opens_at=opened.isoformat(),
        closes_at=closed.isoformat(), label_available_at=observed.isoformat(),
        features=features, outcome=int(raw[7]), baseline=baseline, market=None,
        remaining=(closed - opened).total_seconds(),
        opening_delay_seconds=(generated - opened).total_seconds(),
        regime=opening.get("regime", "unavailable"),
    ), "ELIGIBLE"


def dataset_coverage(db, asof=None):
    """Explain every official outcome's opening-training eligibility.

    Historical inputs are never reconstructed. Missing frozen opening evidence
    is irrecoverable for training because later data would introduce leakage.
    """
    asof = asof or datetime.now(timezone.utc)
    counts = {}
    eligible = []
    for raw in _opening_rows(db, asof):
        row, reason = _assess_opening(raw, asof)
        counts[reason] = counts.get(reason, 0) + 1
        if row is not None:
            eligible.append(row)
    total = sum(counts.values())
    return {
        "official_settled_contracts": total,
        "eligible_frozen_openings": len(eligible),
        "excluded_contracts": total - len(eligible),
        "exclusion_reasons": {k: v for k, v in sorted(counts.items()) if k != "ELIGIBLE"},
        "eligibility_rate": len(eligible) / total if total else None,
        "safe_historical_repairs_applied": 0,
        "repair_policy": "No missing opening features are backfilled from later observations. Only opening-time receipt-proven snapshots may train the opening model.",
        "minimum_for_first_shadow_evaluation": MIN_CONTRACTS,
        "remaining_to_first_shadow_evaluation": max(0, MIN_CONTRACTS - len(eligible)),
        "edge_status": "edge unverified",
    }


def dataset(db, asof=None):
    """Small immutable opening records joined to observed official outcomes only."""
    asof = asof or datetime.now(timezone.utc)
    rows = []
    for raw in _opening_rows(db, asof):
        row, _reason = _assess_opening(raw, asof)
        if row is not None:
            rows.append(row)
    return rows


def _source_receipts_valid(value, cutoff):
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key
                in {
                    "received_at",
                    "observed_at",
                    "event_at",
                    "cutoff",
                    "data_cutoff",
                    "max_input_received_at",
                }
                and child is not None
            ):
                try:
                    if timestamp(child) > cutoff:
                        return False
                except (ValueError, TypeError, AttributeError):
                    return False
            elif isinstance(child, (dict, list)) and not _source_receipts_valid(child, cutoff):
                return False
    elif isinstance(value, list):
        return all(_source_receipts_valid(child, cutoff) for child in value)
    return True


def _boost_raw(model, features):
    vector = learning.transform(features, model["preprocessor"])
    log_odds = model["intercept"]
    for stump in model["stumps"]:
        log_odds += (
            stump["left"] if vector[stump["feature"]] <= stump["threshold"] else stump["right"]
        )
    return learning.sigmoid(log_odds)


def predict(model, features, *, baseline_probability=None):
    if model["kind"] == "opening_gradient_boosting":
        a, b = model.get("calibration", (1.0, 0.0))
        return learning.sigmoid(a * learning.logit(_boost_raw(model, features)) + b)
    if model["kind"] == "opening_ensemble":
        return sum(
            weight * predict(member, features, baseline_probability=baseline_probability)
            for weight, member in zip(model["mixture_weights"], model["components"])
            if weight
        )
    return learning.predict(model, features, baseline_probability=baseline_probability)


def _row_predict(model, row):
    return predict(model, row["features"], baseline_probability=row["baseline"])


def fit_boost(train, prep, rounds=30):
    """Fixed small Newton-boosted stumps; thresholds and leaves see training only."""
    if not train or not prep["names"] or not 1 <= rounds <= 30:
        raise ValueError("Bounded boosting requires usable training features and 1..30 rounds")
    vectors = [learning.transform(row["features"], prep) for row in train]
    ys = [row["outcome"] for row in train]
    intercept = learning.logit((sum(ys) + 1) / (len(ys) + 2))
    logits = [intercept] * len(train)
    splits = []
    # Include missing indicators, exclude the constant intercept.
    for feature in range(len(vectors[0]) - 1):
        values = sorted({v[feature] for v in vectors})
        if len(values) < 2:
            continue
        thresholds = sorted(
            {values[min(len(values) - 2, int(len(values) * q / 8))] for q in range(1, 8)}
        )
        for threshold in thresholds:
            left = [i for i, v in enumerate(vectors) if v[feature] <= threshold]
            left_set = set(left)
            right = [i for i in range(len(train)) if i not in left_set]
            if min(len(left), len(right)) >= 15:
                splits.append((feature, threshold, left, right))
    stumps = []
    for _ in range(rounds):
        ps = [learning.sigmoid(z) for z in logits]
        gradients = [y - p for y, p in zip(ys, ps)]
        hessians = [max(1e-6, p * (1 - p)) for p in ps]
        parent_gain = sum(gradients) ** 2 / (sum(hessians) + 1)
        best = None
        for feature, threshold, left, right in splits:
            lg, rg = sum(gradients[i] for i in left), sum(gradients[i] for i in right)
            lh, rh = sum(hessians[i] for i in left) + 1, sum(hessians[i] for i in right) + 1
            gain = lg * lg / lh + rg * rg / rh - parent_gain
            if best is None or gain > best[0]:
                best = (gain, feature, threshold, 0.1 * lg / lh, 0.1 * rg / rh)
        if best is None or best[0] <= 1e-9:
            break
        _, feature, threshold, left, right = best
        stumps.append(dict(feature=feature, threshold=threshold, left=left, right=right))
        logits = [
            z + (left if vector[feature] <= threshold else right)
            for z, vector in zip(logits, vectors)
        ]
    return dict(
        kind="opening_gradient_boosting",
        preprocessor=prep,
        intercept=intercept,
        stumps=stumps,
        calibration=[1.0, 0.0],
        learning_rate=0.1,
        minimum_leaf=15,
        rounds=len(stumps),
        role="SHADOW_ONLY",
    )


def _calibrate_boost(model, validation):
    zs = [learning.logit(_boost_raw(model, row["features"])) for row in validation]
    ys = [r["outcome"] for r in validation]
    a, b = 1.0, 0.0
    for _ in range(150):
        errors = [learning.sigmoid(a * z + b) - y for z, y in zip(zs, ys)]
        a -= 0.02 * sum(e * z for e, z in zip(errors, zs)) / len(ys)
        b -= 0.02 * sum(errors) / len(ys)
    model["calibration"] = [a, b]


def _ensemble(components, validation):
    ps = [[_row_predict(model, row) for row in validation] for model in components]
    weights = [1 / len(components)] * len(components)
    # Convex log-loss optimization on validation only, fixed iteration budget.
    for _ in range(200):
        mixed = [
            max(1e-6, min(1 - 1e-6, sum(w * p[i] for w, p in zip(weights, ps))))
            for i in range(len(validation))
        ]
        gradient = [
            sum(
                (p - row["outcome"]) / (p * (1 - p)) * column[i]
                for i, (p, row) in enumerate(zip(mixed, validation))
            )
            / len(validation)
            for column in ps
        ]
        updated = [w * math.exp(max(-20, min(20, -0.02 * g))) for w, g in zip(weights, gradient)]
        total = sum(updated)
        weights = [w / total for w in updated]
    return dict(
        kind="opening_ensemble",
        components=components,
        mixture_weights=weights,
        weight_method="Validation-only convex log-loss mirror descent; fixed 200 iterations",
        role="SHADOW_ONLY",
    )


def _pool(train, validation, total_contracts):
    prep = learning.preprocess(train)
    if not prep["names"]:
        return []
    result = []
    for kind in ("logistic", "ridge"):
        for l2 in (0.1, 1.0):
            model = learning.fit(train, kind, l2, prep)
            learning.calibrate(model, validation)
            result.append(
                (
                    score(
                        [_row_predict(model, r) for r in validation],
                        [r["outcome"] for r in validation],
                    ),
                    model,
                )
            )
    members = [
        min(
            (item for item in result if item[1]["kind"] == kind),
            key=lambda item: item[0]["log_loss"],
        )[1]
        for kind in ("logistic", "ridge")
    ]
    if total_contracts >= BOOST_MIN_CONTRACTS:
        boost = fit_boost(train, prep)
        _calibrate_boost(boost, validation)
        result.append(
            (
                score(
                    [_row_predict(boost, r) for r in validation], [r["outcome"] for r in validation]
                ),
                boost,
            )
        )
        members.append(boost)
    members.append({"kind": "volatility_baseline"})
    ensemble = _ensemble(members, validation)
    result.append(
        (
            score(
                [_row_predict(ensemble, r) for r in validation], [r["outcome"] for r in validation]
            ),
            ensemble,
        )
    )
    return result


def _purge(train, validation, test):
    validation_start = min(timestamp(r["timestamp"]) for r in validation)
    test_start = min(timestamp(r["timestamp"]) for r in test)
    return (
        [
            r
            for r in train
            if timestamp(r["closes_at"]) < validation_start
            and timestamp(r["label_available_at"]) <= validation_start
        ],
        [
            r
            for r in validation
            if timestamp(r["closes_at"]) < test_start
            and timestamp(r["label_available_at"]) <= test_start
        ],
        test,
    )


def _counts(groups):
    return [len({r["ticker"] for r in group}) for group in groups]


def _eligible(groups):
    if any(n < minimum for n, minimum in zip(_counts(groups), (100, 30, 30))):
        return "INSUFFICIENT_PURGED_SPLITS"
    if any(
        min(sum(r["outcome"] == y for r in group) for y in (0, 1)) < minimum
        for group, minimum in zip(groups[:2], (10, 5))
    ):
        return "INSUFFICIENT_CLASS_BALANCE"
    return None


def _validate_rows(rows):
    ordered = sorted(rows, key=lambda r: (timestamp(r["closes_at"]), r["ticker"]))
    seen = set()
    for row in ordered:
        if row["ticker"] in seen:
            raise ValueError("Opening evaluation requires exactly one immutable row per contract")
        seen.add(row["ticker"])
        if row["outcome"] not in (0, 1) or not 0 <= row["baseline"] <= 1:
            raise ValueError("Invalid opening labels or baseline")
        if (
            not timestamp(row["timestamp"])
            < timestamp(row["closes_at"])
            <= timestamp(row["label_available_at"])
        ):
            raise ValueError("Invalid opening prediction/label chronology")
    return ordered


def evaluate(rows, used_test_contracts=None, include_walk_forward=False):
    rows = _validate_rows(rows)
    report = dict(
        status="INSUFFICIENT_OPENING_LABELS",
        opening_contracts=len(rows),
        official_contracts=len(rows),
        required_contracts=MIN_CONTRACTS,
        trained=False,
        protocol=PROTOCOL,
        prediction_target="KXBTC15M settlement from frozen opening-cutoff inputs only",
        promotion_allowed=False,
        active_model_changed=False,
        edge_status="edge unverified",
        candidate_catalog={
            "logistic": "Train-only standardized, validation-calibrated",
            "ridge": "Train-only standardized linear probability comparison",
            "ensemble": "Validation-weighted independent linear/volatility candidates",
            "gradient_boosting": f"Bounded calibrated stumps implemented; requires {BOOST_MIN_CONTRACTS} frozen opening contracts before evaluation",
            "random_forest": "Deferred: insufficient frozen opening outcomes for another model family and its selection cost",
            "LightGBM_XGBoost": "Deferred: no evidence supports added dependency/complexity with the current labeled sample",
        },
        method="Opening-only chronological 60/20/20 train/validation/untouched test, label-availability purging; no live-horizon substitution or backfilled features",
    )
    report["holdout_reuse_checked"] = used_test_contracts is not None
    if len(rows) < MIN_CONTRACTS:
        report["shadow_calibration"] = {
            "status": "INSUFFICIENT_OPENING_LABELS",
            "samples": len(rows),
            "methods": ["identity", "monotone Platt", "isotonic PAVA"],
            "active_model_changed": False,
        }
        report[
            "reason"
        ] = "Insufficient newly recorded frozen-feature openings; historical live forecasts cannot replace opening data"
        return None, report
    a, b = int(len(rows) * 0.6), int(len(rows) * 0.8)
    groups = _purge(rows[:a], rows[a:b], rows[b:])
    report["split_contracts"] = _counts(groups)
    blocker = _eligible(groups)
    if blocker:
        report["status"] = blocker
        return None, report
    train, validation, test = groups
    overlap = {r["ticker"] for r in test} & set(used_test_contracts or ())
    if overlap:
        report.update(status="HOLDOUT_REUSE_BLOCKED", holdout_overlap_contracts=len(overlap))
        return None, report
    from .calibration import chronological_calibration, drift_report

    report["shadow_calibration"] = chronological_calibration(
        [{**r, "probability": r["baseline"], "data_cutoff": r["opens_at"]} for r in rows]
    )
    report["feature_drift"] = drift_report(train, test)
    pool = _pool(train, validation, len(rows))
    if not pool:
        report["status"] = "NO_USABLE_OPENING_TRAINING_FEATURES"
        return None, report
    val_metrics, selected = min(pool, key=lambda item: (item[0]["log_loss"], item[0]["brier"]))
    prior = (sum(r["outcome"] for r in train) + 1) / (len(train) + 2)
    val_baseline = score([r["baseline"] for r in validation], [r["outcome"] for r in validation])
    val_prior = score([prior] * len(validation), [r["outcome"] for r in validation])
    # All selection and calibration is complete before the final test is scored.
    ps, ys = [_row_predict(selected, r) for r in test], [r["outcome"] for r in test]
    tested = {
        "selected_candidate": score(ps, ys),
        "opening_statistical_baseline": score([r["baseline"] for r in test], ys),
        "train_prior": score([prior] * len(test), ys),
        "market_implied": learning.market_score(test),
    }
    passed = all(
        val_metrics[k] < min(val_baseline[k], val_prior[k])
        and tested["selected_candidate"][k]
        < min(tested["opening_statistical_baseline"][k], tested["train_prior"][k])
        for k in ("brier", "log_loss")
    )
    report.update(
        status="SHADOW_CANDIDATE_PASSED_STATISTICAL_GATES"
        if passed
        else "EXPERIMENTAL_FALLBACK_RETAINED",
        trained=True,
        selected_before_test=selected["kind"],
        selected_model_fingerprint=hashlib.sha256(
            json.dumps(selected, sort_keys=True).encode()
        ).hexdigest(),
        validation=val_metrics,
        validation_baseline=val_baseline,
        validation_train_prior=val_prior,
        candidate_validation=[
            {"kind": model["kind"], "metrics": metrics} for metrics, model in pool
        ],
        test=tested,
        test_uncertainty=learning.uncertainty(ps, test),
        test_contracts=[r["ticker"] for r in test],
        test_fingerprint=hashlib.sha256(json.dumps(test, sort_keys=True).encode()).hexdigest(),
        train_labels_through=max(r["label_available_at"] for r in train),
        first_validation_prediction=min(r["timestamp"] for r in validation),
        validation_labels_through=max(r["label_available_at"] for r in validation),
        first_test_prediction=min(r["timestamp"] for r in test),
        gradient_boosting_status="EVALUATED_SHADOW_ONLY"
        if len(rows) >= BOOST_MIN_CONTRACTS
        else "DEFERRED_INSUFFICIENT_OPENING_LABELS",
        reason="Shadow evaluation only. A live-horizon artifact is incompatible with opening-cutoff predictions; no model is activated automatically",
    )
    if include_walk_forward:
        report["walk_forward"] = walk_forward(rows[:b])
        report["walk_forward"][
            "scope"
        ] = "Development contracts only; final untouched holdout excluded"
    if not passed:
        return None, report
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "format": "kxbtc-opening-shadow-v1",
        "model": selected,
        "model_id": fingerprint[:16],
        "labels_available_through": max(r["label_available_at"] for r in rows),
        "feature_version": FEATURE_VERSION,
        "protocol": PROTOCOL,
        "report": report,
        "validated": False,
        "created_at": created_at,
        "available_at": created_at,
        "prediction_target": "KXBTC15M_OPENING_SETTLEMENT",
        "forecast_use": "EXPLICIT_CONFIG_ONLY_AFTER_ASOF_CHECK",
        "execution_eligible": False,
        "role": "EXPERIMENTAL_OPENING_CANDIDATE",
        "forecast_activation": "EXPLICIT_CONFIGURATION_ONLY",
        "automatic_activation": False,
        "activation_allowed": False,
    }, report


def _finite(values):
    return all(type(x) in (int, float) and math.isfinite(x) for x in values)


def validate_model(model, component=False):
    if not isinstance(model, dict):
        raise ValueError("Opening model must be a JSON object")
    kind = model.get("kind")
    if kind == "opening_ensemble":
        members, weights = model.get("components"), model.get("mixture_weights")
        if (
            component
            or not isinstance(members, list)
            or not isinstance(weights, list)
            or not 3 <= len(members) <= 4
            or len(members) != len(weights)
            or not _finite(weights)
            or any(not 0 <= w <= 1 for w in weights)
            or abs(sum(weights) - 1) > 1e-9
        ):
            raise ValueError("Unsupported opening ensemble shape or weights")
        kinds = {m.get("kind") for m in members if isinstance(m, dict)}
        if kinds not in (
            {"logistic", "ridge", "volatility_baseline"},
            {"logistic", "ridge", "volatility_baseline", "opening_gradient_boosting"},
        ):
            raise ValueError("Unsupported opening ensemble family")
        for member in members:
            validate_model(member, component=True)
    elif kind == "opening_gradient_boosting":
        prep = model.get("preprocessor", {})
        names, means, scales = (prep.get(k) for k in ("names", "means", "scales"))
        if (
            not all(isinstance(v, list) for v in (names, means, scales))
            or not names
            or len(names) > len(FEATURES)
            or len(set(names)) != len(names)
            or any(n not in FEATURES for n in names)
            or not len(names) == len(means) == len(scales)
            or not _finite(means + scales)
            or any(s <= 0 for s in scales)
        ):
            raise ValueError("Unsupported opening boost preprocessor")
        stumps, calibration = model.get("stumps"), model.get("calibration")
        if (
            not isinstance(stumps, list)
            or len(stumps) > 30
            or not isinstance(calibration, list)
            or len(calibration) != 2
            or not _finite(calibration + [model.get("intercept")])
        ):
            raise ValueError("Unsupported opening boost shape")
        for stump in stumps:
            if (
                not isinstance(stump, dict)
                or set(stump) != {"feature", "threshold", "left", "right"}
                or type(stump["feature"]) is not int
                or not 0 <= stump["feature"] < 2 * len(names)
                or not _finite([stump["threshold"], stump["left"], stump["right"]])
            ):
                raise ValueError("Unsupported opening boost stump")
    else:
        learning.validate_model(model, component=component)


def validate_artifact(data):
    if (
        not isinstance(data, dict)
        or data.get("format") != "kxbtc-opening-shadow-v1"
        or data.get("feature_version") != FEATURE_VERSION
        or data.get("prediction_target") != "KXBTC15M_OPENING_SETTLEMENT"
        or data.get("protocol") != PROTOCOL
        or data.get("forecast_use") != "EXPLICIT_CONFIG_ONLY_AFTER_ASOF_CHECK"
        or data.get("execution_eligible") is not False
        or data.get("forecast_activation") != "EXPLICIT_CONFIGURATION_ONLY"
        or data.get("automatic_activation") is not False
    ):
        raise ValueError("Artifact is not an explicitly opening-targeted forecast candidate")
    validate_model(data["model"])
    report = data["report"]
    counts = report["split_contracts"]
    if (
        report.get("status") != "SHADOW_CANDIDATE_PASSED_STATISTICAL_GATES"
        or report.get("trained") is not True
        or report.get("protocol") != PROTOCOL
        or report.get("holdout_reuse_checked") is not True
        or report.get("selected_before_test") != data["model"]["kind"]
        or report.get("selected_model_fingerprint")
        != hashlib.sha256(json.dumps(data["model"], sort_keys=True).encode()).hexdigest()
        or type(report.get("opening_contracts")) is not int
        or report["opening_contracts"] < MIN_CONTRACTS
        or not isinstance(counts, list)
        or len(counts) != 3
        or any(type(n) is not int or n < minimum for n, minimum in zip(counts, (100, 30, 30)))
        or sum(counts) > report["opening_contracts"]
    ):
        raise ValueError("Opening statistical validation requirements were not met")
    if not timestamp(report["train_labels_through"]) < timestamp(
        report["first_validation_prediction"]
    ) or not timestamp(report["validation_labels_through"]) < timestamp(
        report["first_test_prediction"]
    ):
        raise ValueError("Opening validation chronology is not purged")
    for key in ("brier", "log_loss"):
        values = [
            report["validation"][key],
            report["validation_baseline"][key],
            report["validation_train_prior"][key],
            report["test"]["selected_candidate"][key],
            report["test"]["opening_statistical_baseline"][key],
            report["test"]["train_prior"][key],
        ]
        if (
            not _finite(values)
            or any(x < 0 for x in values)
            or not values[0] < min(values[1:3])
            or not values[3] < min(values[4:])
        ):
            raise ValueError("Opening candidate did not beat predeclared probabilistic baselines")
    if (
        report["validation"].get("samples", 0) < 30
        or report["test"]["selected_candidate"].get("samples", 0) < 30
        or timestamp(data["labels_available_through"]) > timestamp(data["created_at"])
        or timestamp(data["created_at"]) > timestamp(data["available_at"])
        or not isinstance(data.get("model_id"), str)
        or not re.fullmatch(r"[a-f0-9]{16}", data["model_id"])
    ):
        raise ValueError("Opening artifact timing, samples or identity is invalid")
    if data["model"]["kind"] == "opening_gradient_boosting" or any(
        m.get("kind") == "opening_gradient_boosting" for m in data["model"].get("components", [])
    ):
        if report["opening_contracts"] < BOOST_MIN_CONTRACTS:
            raise ValueError("Boosted opening candidate requires larger independent sample")
    return data


def load_artifact(path):
    if path is None or not Path(path).is_file():
        return (
            None,
            "No dedicated opening artifact configured; experimental opening statistical fallback",
        )
    try:
        if Path(path).stat().st_size > 2_000_000:
            raise ValueError("Opening model JSON exceeds bounded size")
        data = validate_artifact(json.loads(Path(path).read_text()))
        data = {**data, "runtime_loaded_at": datetime.now(timezone.utc).isoformat()}
        return (
            data,
            "Opening-specific statistically gated candidate loaded for explicit experimental forecast use; edge unverified",
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return (
            None,
            "Opening artifact rejected: incompatible target, schema, timing or statistical requirements",
        )


def eligible_at_opening(artifact, opened):
    try:
        validate_artifact(artifact)
        return (
            timestamp(artifact["available_at"]) <= opened
            and timestamp(artifact["runtime_loaded_at"]) <= opened
            and timestamp(artifact["labels_available_through"]) < opened
        )
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return False


def walk_forward(rows, development_contracts=170, block_contracts=30):
    rows = _validate_rows(rows)
    result = {
        "status": "INSUFFICIENT_OPENING_LABELS",
        "folds": [],
        "protocol": PROTOCOL,
        "note": "Expanding chronological development; disjoint future blocks, training-only transforms and validation-only calibration. No automatic activation or profitability claim.",
    }
    for index in range(development_contracts, len(rows) - block_contracts + 1, block_contracts):
        split = int(index * 0.8)
        groups = _purge(rows[:split], rows[split:index], rows[index : index + block_contracts])
        if _eligible(groups):
            continue
        train, validation, test = groups
        pool = _pool(train, validation, index)
        if not pool:
            continue
        metrics, model = min(pool, key=lambda item: (item[0]["log_loss"], item[0]["brier"]))
        baseline_val = score(
            [r["baseline"] for r in validation], [r["outcome"] for r in validation]
        )
        use_candidate = all(metrics[k] < baseline_val[k] for k in ("brier", "log_loss"))
        probabilities = [_row_predict(model, r) if use_candidate else r["baseline"] for r in test]
        result["folds"].append(
            {
                "split_contracts": _counts(groups),
                "selected_before_test": model["kind"]
                if use_candidate
                else "opening_statistical_baseline",
                "train_labels_through": max(r["label_available_at"] for r in train),
                "validation_labels_through": max(r["label_available_at"] for r in validation),
                "test_first_prediction": test[0]["timestamp"],
                "test_contracts": [r["ticker"] for r in test],
                "metrics": score(probabilities, [r["outcome"] for r in test]),
                "baseline": score([r["baseline"] for r in test], [r["outcome"] for r in test]),
            }
        )
    if result["folds"]:
        result["status"] = "SHADOW_WALK_FORWARD_COMPLETE"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--output", type=Path, default=Path("opening_shadow.json"))
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("opening_shadow_registry"),
        help="Separate opening holdout-exposure ledger; it cannot activate a model",
    )
    args = parser.parse_args()
    try:
        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = dataset(db)
        from .model_registry import atomic_json, ModelRegistry

        registry = ModelRegistry(args.registry)
        with registry.update_lock():
            artifact, report = evaluate(
                rows,
                used_test_contracts=registry.used_test_contracts(),
                include_walk_forward=args.walk_forward,
            )
            atomic_json(args.output.with_suffix(".report.json"), report)
            registry.record_evaluation(report, datetime.now(timezone.utc))
            if artifact:
                atomic_json(args.output, artifact)
        print(
            f"{report['status']}: {report['opening_contracts']} eligible frozen-feature openings; shadow only, edge unverified"
        )
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

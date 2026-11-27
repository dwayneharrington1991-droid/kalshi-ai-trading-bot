"""Shadow-only rigorous opening-model evaluation. No network, account or activation.

This module adds a stricter, separately versioned evaluation protocol beside the
existing opening pipeline. It never edits, replaces, registers or activates any
existing model, artifact, registry or journal record:

* journals are opened read-only;
* it writes only its own report and its own append-only evaluation ledger;
* its report format is deliberately incompatible with
  ``opening_models.validate_artifact`` so it cannot be loaded as a forecast.

Protocol (all choices predeclared here, before any data is read):

1. One immutable frozen opening row per contract, chronological
   train / validation / untouched forward test with label-availability purging
   (reusing ``learning.chronological_contract_partitions`` and
   ``opening_models._purge``).
2. Candidate fitting uses training rows only. The L2 strength is chosen by
   expanding-window time-series cross-validation *inside* the training block.
   Logistic models are fitted by damped Newton iterations to a gradient
   tolerance with an un-normalised L2 penalty, so the penalty grid matters.
3. Validation is split chronologically into V1 and V2. Calibration
   (``calibration.fit_platt``) is fitted on V1 only and candidates are ranked on
   V2 only, so no validation row is used both to fit and to rank.
4. Exactly one ranked candidate reaches the gate. Approval needs lower test log
   loss and Brier than both the opening statistical baseline and the training
   prior, and a one-sided Harvey-Leybourne-Newbold Diebold-Mariano test of the
   per-contract log-loss differences against *both* references (an
   intersection-union test, so requiring both adds no multiplicity inflation).
5. Repeated evaluations spend alpha: attempt k is tested at
   ``FAMILY_ALPHA * 6 / (pi**2 * k**2)``, so all attempts together are bounded
   by ``FAMILY_ALPHA``. Previously exposed test contracts are never re-tested.

Passing these gates is statistical shadow evidence only. It never establishes a
profitable edge and never changes the active forecast.
"""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3

from . import learning, opening_models
from .calibration import apply_calibration, fit_platt
from .features import FEATURES
from .model import timestamp
from .scores import score

PROTOCOL = "opening-rigorous-crossfit-hln-dm-alphaspend-shadow-v1"
REPORT_FORMAT = "kxbtc-opening-rigorous-shadow-report-v1"
LEDGER_FORMAT = "kxbtc-opening-rigorous-shadow-ledger-v1"
FAMILY_ALPHA = 0.05
MIN_TRAIN_CONTRACTS = 100
MIN_CALIBRATION_CONTRACTS = 15
MIN_RANKING_CONTRACTS = 15
MIN_TEST_CONTRACTS = 30
LAMBDA_GRID = (0.3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0)
TIME_SERIES_FOLDS = 3
NEWTON_TOLERANCE = 1e-8
NEWTON_MAX_ITERATIONS = 100
BASELINE = "opening_statistical_baseline"
CANDIDATES = ("compact_offset_logistic", "compact_logistic")
# Confidence = calibrated probability of the called side, max(p, 1 - p).
CONFIDENCE_BANDS = (
    ("50-60%", 0.5, 0.6),
    ("60-70%", 0.6, 0.7),
    ("70-80%", 0.7, 0.8),
    ("80-90%", 0.8, 0.9),
    ("90%+", 0.9, 1.0 + 1e-12),
)
MIN_BAND_CONTRACTS = 30
HIGH_CONFIDENCE_TARGET = 0.80

# Predeclared compact opening vocabulary. Every name is produced by
# prediction_engine._opening_features from receipt-proven pre-opening inputs.
# Fixed before evaluation; never selected from data.
COMPACT_FEATURES = (
    "return_1m",
    "return_5m",
    "return_15m",
    "return_60m",
    "return_24h",
    "acceleration",
    "body_fraction",
    "breakout_distance",
    "volatility_change",
    "relative_volume",
    "spot_depth_imbalance",
    "derivatives_basis",
)
if any(name not in FEATURES for name in COMPACT_FEATURES):  # pragma: no cover - import guard
    raise ImportError("Compact opening features must be a subset of FEATURES")


# ---------------------------------------------------------------------------
# Statistics (pure Python; production evaluators may not install SciPy)
# ---------------------------------------------------------------------------


def _beta_continued_fraction(a, b, x):
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1.0)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    result = d
    for m in range(1, 400):
        m2 = 2 * m
        for numerator in (
            m * (b - m) * x / ((a + m2 - 1.0) * (a + m2)),
            -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1.0)),
        ):
            d = 1.0 + numerator * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + numerator / c
            c = c if abs(c) > tiny else tiny
            result *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return result


def regularized_incomplete_beta(a, b, x):
    if not 0 <= x <= 1 or a <= 0 or b <= 0:
        raise ValueError("Invalid incomplete beta arguments")
    if x in (0, 1):
        return float(x)
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_front) * _beta_continued_fraction(a, b, x) / a
    return 1.0 - math.exp(log_front) * _beta_continued_fraction(b, a, 1.0 - x) / b


def student_t_cdf(t, df):
    if df <= 0 or not math.isfinite(df):
        raise ValueError("Degrees of freedom must be positive and finite")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t > 0 else tail


def alpha_for_attempt(attempt, family_alpha=FAMILY_ALPHA):
    """Alpha spent on the k-th evaluation; the infinite sum equals family_alpha."""
    if type(attempt) is not int or attempt < 1:
        raise ValueError("Attempt numbers start at 1")
    return family_alpha * 6.0 / (math.pi**2 * attempt**2)


def paired_superiority_test(candidate_losses, reference_losses):
    """One-sided HLN-corrected Diebold-Mariano test, H1: candidate loss is lower.

    Differences are per independent contract in chronological order. A
    Newey-West (Bartlett) long-run variance accounts for serial dependence
    between neighbouring 15-minute contracts.
    """
    if len(candidate_losses) != len(reference_losses):
        raise ValueError("Paired loss series must have equal length")
    d = [r - c for c, r in zip(candidate_losses, reference_losses)]
    n = len(d)
    if n < 2 or any(not math.isfinite(x) for x in d):
        return {"samples": n, "mean_improvement": None, "statistic": None, "p_value": 1.0}
    mean = sum(d) / n
    lags = min(n - 1, int(n ** (1.0 / 3.0)))
    centered = [x - mean for x in d]
    variance = sum(x * x for x in centered) / n
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        variance += 2.0 * weight * sum(
            centered[i] * centered[i - lag] for i in range(lag, n)
        ) / n
    if variance <= 1e-18:
        p_value = 0.0 if mean > 0 else 1.0
        statistic = math.inf if mean > 0 else -math.inf if mean < 0 else 0.0
    else:
        statistic = mean / math.sqrt(variance / n) * math.sqrt((n - 1.0) / n)
        p_value = 1.0 - student_t_cdf(statistic, n - 1)
    return {
        "samples": n,
        "mean_improvement": mean,
        "statistic": statistic if math.isfinite(statistic) else None,
        "p_value": max(0.0, min(1.0, p_value)),
        "newey_west_lags": lags,
        "test": "one-sided Harvey-Leybourne-Newbold Diebold-Mariano, Student t(n-1)",
    }


def _log_losses(probabilities, outcomes):
    losses = []
    for p, y in zip(probabilities, outcomes):
        p = max(1e-9, min(1 - 1e-9, p))
        losses.append(-math.log(p) if y else -math.log(1 - p))
    return losses


def _brier_losses(probabilities, outcomes):
    return [(p - y) ** 2 for p, y in zip(probabilities, outcomes)]


def _band_summary(selected, total):
    from .scores import wilson_interval

    n = len(selected)
    correct = sum((p >= 0.5) == bool(y) for p, y in selected)
    confidences = [max(p, 1 - p) for p, _ in selected]
    mean_confidence = sum(confidences) / n if n else None
    interval = wilson_interval(correct, n) if n else None
    if n < MIN_BAND_CONTRACTS:
        status = "INSUFFICIENT_SAMPLE"
    elif interval[0] <= mean_confidence <= interval[1]:
        status = "OBSERVED_ACCURACY_CONSISTENT_WITH_STATED_CONFIDENCE"
    else:
        status = "STATED_CONFIDENCE_MISCALIBRATED"
    return {
        "contracts": n,
        "coverage": n / total if total else 0.0,
        "up_calls": sum(p >= 0.5 for p, _ in selected),
        "down_calls": sum(p < 0.5 for p, _ in selected),
        "correct": correct,
        "observed_accuracy": correct / n if n else None,
        "accuracy_wilson_95": interval,
        "mean_stated_confidence": mean_confidence,
        "calibration_gap": (correct / n - mean_confidence) if n else None,
        "status": status,
    }


def confidence_band_report(probabilities, rows):
    """Observed accuracy by calibrated-confidence band on one row per contract.

    Bands are fixed in advance; nothing here selects a model or threshold. A
    band with fewer than MIN_BAND_CONTRACTS contracts reports
    INSUFFICIENT_SAMPLE instead of an accuracy claim. The 80% objective counts
    as demonstrated only when the cumulative >=80% group has enough contracts
    and the lower 95% Wilson bound of its accuracy is itself at least 80%.
    """
    pairs = [(float(p), int(r["outcome"])) for p, r in zip(probabilities, rows)]
    total = len(pairs)
    bands = {
        label: _band_summary([(p, y) for p, y in pairs if low <= max(p, 1 - p) < high], total)
        for label, low, high in CONFIDENCE_BANDS
    }
    selective = {
        f">={int(low * 100)}%": _band_summary([(p, y) for p, y in pairs if max(p, 1 - p) >= low], total)
        for _label, low, _high in CONFIDENCE_BANDS[1:]
    }
    high = selective[f">={int(HIGH_CONFIDENCE_TARGET * 100)}%"]
    return {
        "contracts": total,
        "bands": bands,
        "selective_abstention": selective,
        "highest_stated_confidence": max((max(p, 1 - p) for p, _ in pairs), default=None),
        "high_confidence_target": HIGH_CONFIDENCE_TARGET,
        "high_confidence_target_demonstrated": bool(
            high["contracts"] >= MIN_BAND_CONTRACTS
            and high["accuracy_wilson_95"][0] >= HIGH_CONFIDENCE_TARGET
        ),
        "rule": (
            "Demonstrated only if >=30 untouched-test contracts have calibrated confidence "
            ">=80% and the lower 95% Wilson bound of their accuracy is >=80%. Bands are "
            "descriptive; serial dependence can make intervals optimistic."
        ),
    }


# ---------------------------------------------------------------------------
# Converged, meaningfully regularised logistic regression
# ---------------------------------------------------------------------------


def _solve(matrix, vector):
    size = len(vector)
    a = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(a[r][column]))
        if abs(a[pivot][column]) < 1e-14:
            raise ValueError("Singular Newton system")
        a[column], a[pivot] = a[pivot], a[column]
        for row in range(column + 1, size):
            factor = a[row][column] / a[column][column]
            if factor:
                for k in range(column, size + 1):
                    a[row][k] -= factor * a[column][k]
    solution = [0.0] * size
    for row in range(size - 1, -1, -1):
        solution[row] = (
            a[row][size] - sum(a[row][k] * solution[k] for k in range(row + 1, size))
        ) / a[row][row]
    return solution


def _offset(row, use_offset):
    return learning.logit(row["baseline"]) if use_offset else 0.0


def _objective(weights, xs, offsets, ys, l2):
    total = 0.0
    for x, offset, y in zip(xs, offsets, ys):
        z = offset + sum(w * v for w, v in zip(weights, x))
        total += max(z, 0.0) - y * z + math.log1p(math.exp(-abs(z)))
    return total + 0.5 * l2 * sum(w * w for w in weights[:-1])


def fit_logistic(rows, prep, l2, *, use_offset):
    """Penalised logistic MLE by damped Newton. Intercept is unpenalised.

    Objective: sum of log losses + l2/2 * ||w_without_intercept||^2. The penalty
    is not divided by the sample size, so each grid value changes the fit.
    """
    if not rows or l2 <= 0 or not math.isfinite(l2):
        raise ValueError("Rows and a positive finite L2 strength are required")
    xs = [learning.transform(r["features"], prep) for r in rows]
    ys = [r["outcome"] for r in rows]
    offsets = [_offset(r, use_offset) for r in rows]
    dim = len(xs[0])
    weights = [0.0] * dim
    converged = False
    gradient_norm = math.inf
    iteration = 0
    for iteration in range(1, NEWTON_MAX_ITERATIONS + 1):
        gradient = [l2 * w for w in weights[:-1]] + [0.0]
        hessian = [[0.0] * dim for _ in range(dim)]
        for j in range(dim - 1):
            hessian[j][j] = l2
        for x, offset, y in zip(xs, offsets, ys):
            p = learning.sigmoid(offset + sum(w * v for w, v in zip(weights, x)))
            residual, curvature = p - y, max(p * (1 - p), 1e-12)
            for j, xj in enumerate(x):
                if xj:
                    gradient[j] += residual * xj
                    row = hessian[j]
                    for k, xk in enumerate(x):
                        if xk:
                            row[k] += curvature * xj * xk
        gradient_norm = max(abs(g) for g in gradient)
        if gradient_norm < NEWTON_TOLERANCE * max(1, len(rows)):
            converged = True
            break
        step = _solve(hessian, gradient)
        current = _objective(weights, xs, offsets, ys, l2)
        rate = 1.0
        for _ in range(40):
            proposal = [w - rate * s for w, s in zip(weights, step)]
            if _objective(proposal, xs, offsets, ys, l2) <= current + 1e-12:
                break
            rate /= 2
        weights = proposal
        if max(abs(rate * s) for s in step) < 1e-12:
            converged = True
            break
    return {
        "kind": "rigorous_offset_logistic" if use_offset else "rigorous_logistic",
        "weights": weights,
        "preprocessor": prep,
        "l2": l2,
        "use_baseline_offset": use_offset,
        "converged": converged,
        "newton_iterations": iteration,
        "final_gradient_max_abs": gradient_norm,
        "calibration": {"method": "identity_v1"},
        "role": "SHADOW_ONLY",
    }


def raw_probability(model, row):
    x = learning.transform(row["features"], model["preprocessor"])
    z = _offset(row, model["use_baseline_offset"]) + sum(
        w * v for w, v in zip(model["weights"], x)
    )
    return learning.sigmoid(z)


def predict(model, row):
    return apply_calibration(model["calibration"], raw_probability(model, row))


def compact_preprocessor(train):
    reduced = [
        {**r, "features": {k: v for k, v in r["features"].items() if k in COMPACT_FEATURES}}
        for r in train
    ]
    return learning.preprocess(reduced)


def choose_l2(train, *, use_offset, folds=TIME_SERIES_FOLDS):
    """Expanding-window time-series CV inside training rows only."""
    blocks = folds + 1
    size = len(train) // blocks
    if size < 10:
        raise ValueError("Training block too small for time-series L2 selection")
    results = []
    for l2 in LAMBDA_GRID:
        losses = []
        for fold in range(1, blocks):
            fit_rows = train[: fold * size]
            held = train[fold * size : (fold + 1) * size if fold < folds else len(train)]
            start = min(timestamp(r["timestamp"]) for r in held)
            fit_rows = [
                r for r in fit_rows
                if timestamp(r["label_available_at"]) <= start
                and timestamp(r["closes_at"]) < start
            ]
            prep = compact_preprocessor(fit_rows)
            if not prep["names"] or len(fit_rows) < 10:
                continue
            model = fit_logistic(fit_rows, prep, l2, use_offset=use_offset)
            losses.extend(
                _log_losses([raw_probability(model, r) for r in held], [r["outcome"] for r in held])
            )
        if losses:
            results.append((sum(losses) / len(losses), -l2, l2))
    if not results:
        raise ValueError("No usable compact training features")
    # Ties favour the stronger penalty (simpler model).
    best = min(results)
    return best[2], [{"l2": l2, "mean_log_loss": loss} for loss, _neg, l2 in sorted(results, key=lambda r: r[2])]


# ---------------------------------------------------------------------------
# Selection without validation-row reuse
# ---------------------------------------------------------------------------


def split_validation(validation):
    """Chronological V1 (calibration fit) / V2 (ranking) with purging."""
    half = len(validation) // 2
    v1, v2 = validation[:half], validation[half:]
    if not v1 or not v2:
        return v1, v2
    start = min(timestamp(r["timestamp"]) for r in v2)
    v1 = [
        r for r in v1
        if timestamp(r["label_available_at"]) <= start and timestamp(r["closes_at"]) < start
    ]
    return v1, v2


def _calibrated(model, rows):
    raw = [raw_probability(model, r) for r in rows]
    platt = fit_platt(raw, [r["outcome"] for r in rows])
    return {**model, "calibration": {"method": "platt_monotone_v1", "slope": platt["slope"], "intercept": platt["intercept"], "samples": platt["samples"], "regularization": platt["regularization"]}}


def build_candidates(train, v1, v2):
    """Fit on train, calibrate on V1, rank on V2. Returns (ranking, fitted)."""
    fitted = {}
    ranking = []
    y2 = [r["outcome"] for r in v2]
    for name in CANDIDATES:
        use_offset = name == "compact_offset_logistic"
        l2, grid = choose_l2(train, use_offset=use_offset)
        prep = compact_preprocessor(train)
        model = fit_logistic(train, prep, l2, use_offset=use_offset)
        model["l2_selection"] = grid
        model["candidate"] = name
        calibrated_v1 = _calibrated(model, v1)
        metrics = score([predict(calibrated_v1, r) for r in v2], y2)
        fitted[name] = model
        ranking.append({"candidate": name, "l2": l2, "ranking_metrics": metrics})
    ranking.append(
        {"candidate": BASELINE, "l2": None, "ranking_metrics": score([r["baseline"] for r in v2], y2)}
    )
    ranking.sort(key=lambda item: (item["ranking_metrics"]["log_loss"], item["ranking_metrics"]["brier"]))
    return ranking, fitted


def existing_pipeline_candidate(train, validation, total_contracts):
    """The unchanged existing opening selection, for head-to-head diagnostics."""
    pool = opening_models._pool(train, validation, total_contracts)
    if not pool:
        return None, None
    metrics, model = min(pool, key=lambda item: (item[0]["log_loss"], item[0]["brier"]))
    return model, metrics


def _gate(probabilities, test, prior, alpha):
    ys = [r["outcome"] for r in test]
    baseline = [r["baseline"] for r in test]
    priors = [prior] * len(test)
    candidate_ll = _log_losses(probabilities, ys)
    versus_baseline = paired_superiority_test(candidate_ll, _log_losses(baseline, ys))
    versus_prior = paired_superiority_test(candidate_ll, _log_losses(priors, ys))
    candidate_brier = sum(_brier_losses(probabilities, ys)) / len(ys)
    point = (
        candidate_brier < sum(_brier_losses(baseline, ys)) / len(ys)
        and candidate_brier < sum(_brier_losses(priors, ys)) / len(ys)
        and sum(candidate_ll) / len(ys) < min(
            sum(_log_losses(baseline, ys)) / len(ys), sum(_log_losses(priors, ys)) / len(ys)
        )
    )
    significant = versus_baseline["p_value"] <= alpha and versus_prior["p_value"] <= alpha
    return {
        "point_estimates_beat_both_references": point,
        "log_loss_vs_opening_baseline": versus_baseline,
        "log_loss_vs_train_prior": versus_prior,
        "alpha_spent": alpha,
        "passed": bool(point and significant and len(test) >= MIN_TEST_CONTRACTS),
    }


def _existing_gate_verdict(val_metrics, validation, test, test_probabilities, prior):
    """Replicates opening_models.evaluate's point-estimate gate for comparison."""
    yv, yt = [r["outcome"] for r in validation], [r["outcome"] for r in test]
    val_base = score([r["baseline"] for r in validation], yv)
    val_prior = score([prior] * len(validation), yv)
    tested = score(test_probabilities, yt)
    test_base = score([r["baseline"] for r in test], yt)
    test_prior = score([prior] * len(test), yt)
    return all(
        val_metrics[k] < min(val_base[k], val_prior[k])
        and tested[k] < min(test_base[k], test_prior[k])
        for k in ("brier", "log_loss")
    )


def evaluate(rows, *, used_test_contracts=None, attempt=1, include_existing=True):
    """Shadow evaluation. Returns a report; never an activatable artifact."""
    rows = opening_models._validate_rows(rows)
    report = {
        "format": REPORT_FORMAT,
        "protocol": PROTOCOL,
        "status": "INSUFFICIENT_OPENING_LABELS",
        "opening_contracts": len(rows),
        "compact_features": list(COMPACT_FEATURES),
        "family_alpha": FAMILY_ALPHA,
        "attempt": attempt,
        "alpha_spent": alpha_for_attempt(attempt),
        "role": "SHADOW_ONLY",
        "promotion_allowed": False,
        "activation_allowed": False,
        "automatic_activation": False,
        "active_model_changed": False,
        "execution_eligible": False,
        "edge_status": "edge unverified",
        "test_exposed": False,
    }
    if len(rows) < learning.MIN_CONTRACTS:
        return report
    contract_sets, partition = learning.chronological_contract_partitions(
        [r["ticker"] for r in rows], used_test_contracts
    )
    report["partition"] = partition
    if contract_sets is None:
        report["status"] = "INSUFFICIENT_UNSEEN_FORWARD_HOLDOUT"
        return report
    train, validation, test = opening_models._purge(
        *[[r for r in rows if r["ticker"] in group] for group in contract_sets]
    )
    overlap = {r["ticker"] for r in test} & set(used_test_contracts or ())
    if overlap:
        raise AssertionError("Rigorous evaluation reused an exposed test contract")
    v1, v2 = split_validation(validation)
    counts = {"train": len(train), "calibration_v1": len(v1), "ranking_v2": len(v2), "test": len(test)}
    report["split_contracts"] = counts
    if (
        counts["train"] < MIN_TRAIN_CONTRACTS
        or counts["calibration_v1"] < MIN_CALIBRATION_CONTRACTS
        or counts["ranking_v2"] < MIN_RANKING_CONTRACTS
        or counts["test"] < MIN_TEST_CONTRACTS
    ):
        report["status"] = "INSUFFICIENT_PURGED_SPLITS"
        return report
    if min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10:
        report["status"] = "INSUFFICIENT_CLASS_BALANCE"
        return report
    report["chronology"] = {
        "train_labels_through": max(r["label_available_at"] for r in train),
        "calibration_first_prediction": min(r["timestamp"] for r in v1),
        "calibration_labels_through": max(r["label_available_at"] for r in v1),
        "ranking_first_prediction": min(r["timestamp"] for r in v2),
        "ranking_labels_through": max(r["label_available_at"] for r in v2),
        "test_first_prediction": min(r["timestamp"] for r in test),
    }
    try:
        ranking, fitted = build_candidates(train, v1, v2)
    except ValueError as exc:
        report.update(status="NO_USABLE_COMPACT_FEATURES", reason=str(exc))
        return report
    selected = ranking[0]["candidate"]
    report["ranking_on_v2"] = ranking
    report["selected_before_test"] = selected
    prior = (sum(r["outcome"] for r in train) + 1) / (len(train) + 2)
    ys = [r["outcome"] for r in test]
    # ----- every selection decision is frozen above this line -----
    report["test_exposed"] = True
    report["test_contracts"] = [r["ticker"] for r in test]
    tested = {
        BASELINE: score([r["baseline"] for r in test], ys),
        "train_prior": score([prior] * len(test), ys),
    }
    final_models = {}
    for name, model in fitted.items():
        final = _calibrated(model, validation)  # calibration only; ranking already frozen
        final_models[name] = final
        tested[name] = score([predict(final, r) for r in test], ys)
    report["test"] = tested
    report["confidence_bands"] = {
        BASELINE: confidence_band_report([r["baseline"] for r in test], test),
        **{
            name: confidence_band_report([predict(final, r) for r in test], test)
            for name, final in final_models.items()
        },
    }
    if selected == BASELINE:
        report["status"] = "BASELINE_RETAINED_ON_RANKING"
        report["gate"] = None
    else:
        probabilities = [predict(final_models[selected], r) for r in test]
        report["gate"] = _gate(probabilities, test, prior, report["alpha_spent"])
        report["status"] = (
            "SHADOW_PASSED_RIGOROUS_GATES" if report["gate"]["passed"] else "SHADOW_FAILED_RIGOROUS_GATES"
        )
        report["selected_model"] = final_models[selected]
    report["convergence"] = {
        name: {k: m[k] for k in ("converged", "newton_iterations", "final_gradient_max_abs", "l2")}
        for name, m in fitted.items()
    }
    if include_existing:
        model, val_metrics = existing_pipeline_candidate(train, validation, len(rows))
        if model is not None:
            probabilities = [opening_models._row_predict(model, r) for r in test]
            report["existing_pipeline_comparison"] = {
                "selected_kind": model["kind"],
                "selected_feature_set": model.get("feature_set", "ensemble"),
                "validation_reused_for_fit_and_rank": True,
                "test": score(probabilities, ys),
                "confidence_bands": confidence_band_report(probabilities, test),
                "existing_point_gate_passed": _existing_gate_verdict(
                    val_metrics, validation, test, probabilities, prior
                ),
                "rigorous_gate_applied_to_existing": _gate(
                    probabilities, test, prior, report["alpha_spent"]
                ),
                "note": "Existing pipeline unchanged; scored on the same untouched test for comparison only",
            }
    return report


def walk_forward(rows, development_contracts=170, block_contracts=30):
    """Expanding chronological folds; each fold repeats the full protocol."""
    rows = opening_models._validate_rows(rows)
    folds = []
    for end in range(development_contracts, len(rows) - block_contracts + 1, block_contracts):
        subset = rows[: end + block_contracts]
        dev = [r["ticker"] for r in rows[:end]]
        split = int(len(dev) * 0.8)
        sets = [set(dev[:split]), set(dev[split:]), {r["ticker"] for r in rows[end : end + block_contracts]}]
        train, validation, test = opening_models._purge(
            *[[r for r in subset if r["ticker"] in group] for group in sets]
        )
        v1, v2 = split_validation(validation)
        if min(len(train), len(v1), len(v2), len(test)) < 10:
            continue
        try:
            ranking, fitted = build_candidates(train, v1, v2)
        except ValueError:
            continue
        selected = ranking[0]["candidate"]
        ys = [r["outcome"] for r in test]
        if selected == BASELINE:
            probabilities = [r["baseline"] for r in test]
        else:
            probabilities = [predict(_calibrated(fitted[selected], validation), r) for r in test]
        folds.append({
            "test_first_prediction": test[0]["timestamp"],
            "selected_before_test": selected,
            "test_log_losses": _log_losses(probabilities, ys),
            "baseline_log_losses": _log_losses([r["baseline"] for r in test], ys),
        })
    candidate = [x for fold in folds for x in fold.pop("test_log_losses")]
    reference = [x for fold in folds for x in fold.pop("baseline_log_losses")]
    return {
        "status": "SHADOW_WALK_FORWARD_COMPLETE" if folds else "INSUFFICIENT_OPENING_LABELS",
        "folds": folds,
        "pooled_out_of_sample": paired_superiority_test(candidate, reference) if candidate else None,
        "note": "Diagnostic only: overlapping development reuse across folds; not an approval gate",
    }


# ---------------------------------------------------------------------------
# Separate ledger (never the existing model registry)
# ---------------------------------------------------------------------------


class RigorousLedger:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def lock(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def state(self):
        if not self.path.exists():
            return {"format": LEDGER_FORMAT, "protocol": PROTOCOL, "attempts": []}
        data = json.loads(self.path.read_text())
        if data.get("format") != LEDGER_FORMAT or not isinstance(data.get("attempts"), list):
            raise ValueError("Refusing to use a file that is not a rigorous shadow ledger")
        return data

    def next_attempt(self):
        return 1 + sum(1 for item in self.state()["attempts"] if item.get("test_exposed"))

    def used_test_contracts(self):
        return {t for item in self.state()["attempts"] for t in item.get("test_contracts", [])}

    def record(self, report, now):
        from .model_registry import atomic_json

        data = self.state()
        data["attempts"].append({
            "evaluated_at": now.isoformat(),
            "attempt": report.get("attempt"),
            "status": report.get("status"),
            "alpha_spent": report.get("alpha_spent"),
            "test_exposed": bool(report.get("test_exposed")),
            "test_contracts": report.get("test_contracts", []),
        })
        atomic_json(self.path, data)


def _refuse_foreign_file(path, expected_format):
    path = Path(path)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"Refusing to overwrite unrecognised file {path}") from exc
        if not isinstance(existing, dict) or existing.get("format") != expected_format:
            raise ValueError(f"Refusing to overwrite {path}: not a rigorous shadow file")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--output", type=Path, default=Path("opening_rigorous_shadow.report.json"))
    parser.add_argument("--ledger", type=Path, default=Path("opening_rigorous_shadow_ledger.json"))
    parser.add_argument("--walk-forward", action="store_true")
    args = parser.parse_args()
    try:
        _refuse_foreign_file(args.output, REPORT_FORMAT)
        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = opening_models.dataset(db)
        ledger = RigorousLedger(args.ledger)
        from .model_registry import atomic_json

        with ledger.lock():
            report = evaluate(
                rows, used_test_contracts=ledger.used_test_contracts(), attempt=ledger.next_attempt()
            )
            if args.walk_forward:
                # Development contracts only: the untouched test block and
                # every previously exposed holdout stay excluded.
                excluded = set(report.get("test_contracts", [])) | ledger.used_test_contracts()
                report["walk_forward"] = walk_forward(
                    [r for r in rows if r["ticker"] not in excluded]
                )
                report["walk_forward"]["scope"] = "Development contracts only; holdouts excluded"
            atomic_json(args.output, report)
            ledger.record(report, datetime.now(timezone.utc))
        print(f"{report['status']}: {report['opening_contracts']} frozen openings; shadow only, edge unverified")
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

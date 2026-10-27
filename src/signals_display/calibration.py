"""Chronological, shadow-only probability calibration and distribution checks.

This module neither trains the underlying forecast nor activates a model. The
caller remains responsible for supplying genuine, pre-outcome predictions and
confirmed official labels; synthetic data must never be reported as live skill.
"""
from bisect import bisect_right
from datetime import datetime, timezone
import math

from .model import timestamp
from .scores import score


UNCERTAINTY_NOTE = (
    "Reliability bins and Wilson/approximate loss intervals are descriptive. "
    "Serial dependence, small samples and repeated model selection can invalidate "
    "nominal coverage. Calibration does not demonstrate a profitable edge."
)


def _pairs(probabilities, outcomes):
    if len(probabilities) != len(outcomes):
        raise ValueError("Probability and outcome counts must match")
    pairs = []
    for p, y in zip(probabilities, outcomes):
        if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("Probability must be a finite number in [0,1]")
        if type(y) not in (int, float) or y not in (0, 1):
            raise ValueError("Confirmed outcomes must be binary")
        pairs.append((float(p), int(y)))
    return pairs


def fit_isotonic(probabilities, outcomes):
    """Weighted pool-adjacent-violators fit, combining tied probabilities first.

    Only observations explicitly passed to this function are fitted. Returned
    knots interpolate linearly, with constant extension outside the fit range.
    """
    pairs = sorted(_pairs(probabilities, outcomes))
    if not pairs:
        raise ValueError("At least one calibration observation is required")
    grouped = []
    for p, y in pairs:
        if grouped and grouped[-1][0] == p:
            grouped[-1][1] += y
            grouped[-1][2] += 1
        else:
            grouped.append([p, y, 1])
    blocks = []
    for i, (_, positives, count) in enumerate(grouped):
        blocks.append([i, i, positives, count])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    fitted = [0.0] * len(grouped)
    for first, last, positives, count in blocks:
        for i in range(first, last + 1):
            fitted[i] = positives / count
    return {
        "method": "isotonic_pava_v1",
        "x": [row[0] for row in grouped],
        "y": fitted,
        "samples": len(pairs),
        "blocks": len(blocks),
        "both_classes": len({y for _, y in pairs}) == 2,
        "extrapolation": "constant endpoints; linear interpolation between knots",
    }


def _logit(p):
    p = max(1e-7, min(1 - 1e-7, p))
    return math.log(p / (1 - p))


def _sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-min(z, 700)))
    ez = math.exp(max(z, -700))
    return ez / (1 + ez)


def fit_platt(probabilities, outcomes, *, regularization=0.01):
    """Regularized two-parameter Platt fit, preserving probability ordering.

    The nonnegative slope constraint avoids silently turning a calibrator into
    an opposite-direction forecasting model. Identity is the regularization prior.
    """
    pairs = _pairs(probabilities, outcomes)
    if not pairs:
        raise ValueError("At least one calibration observation is required")
    if not math.isfinite(regularization) or regularization <= 0:
        raise ValueError("Positive finite regularization is required")
    values = [(_logit(p), y) for p, y in pairs]
    n = len(values)
    a, b = 1.0, 0.0

    def objective(aa, bb):
        losses = []
        for z, y in values:
            t = aa * z + bb
            losses.append(max(t, 0) - y * t + math.log1p(math.exp(-abs(t))))
        return sum(losses) / n + regularization * ((aa - 1) ** 2 + bb**2) / 2

    for _ in range(100):
        ga, gb = regularization * (a - 1), regularization * b
        haa, hab, hbb = regularization, 0.0, regularization
        for z, y in values:
            p = _sigmoid(a * z + b)
            w = p * (1 - p) / n
            ga += (p - y) * z / n
            gb += (p - y) / n
            haa += w * z * z
            hab += w * z
            hbb += w
        determinant = haa * hbb - hab * hab
        if determinant <= 0:
            break
        da = (hbb * ga - hab * gb) / determinant
        db = (haa * gb - hab * ga) / determinant
        previous = objective(a, b)
        rate = 1.0
        for _ in range(30):
            new_a, new_b = max(0.0, min(50.0, a - rate * da)), max(-50.0, min(50.0, b - rate * db))
            if objective(new_a, new_b) <= previous + 1e-12:
                break
            rate /= 2
        difference = abs(new_a - a) + abs(new_b - b)
        a, b = new_a, new_b
        if difference < 1e-8:
            break
    return {
        "method": "platt_monotone_v1",
        "slope": a,
        "intercept": b,
        "regularization": regularization,
        "samples": n,
        "both_classes": len({y for _, y in pairs}) == 2,
    }


def apply_calibration(artifact, probability):
    """Apply a bounded JSON artifact; unknown or malformed methods fail closed."""
    p = _pairs([probability], [0])[0][0]
    if not isinstance(artifact, dict):
        raise ValueError("Calibration artifact must be a mapping")
    method = artifact.get("method")
    if method == "identity_v1":
        return p
    if method == "platt_monotone_v1":
        a, b = artifact.get("slope"), artifact.get("intercept")
        if (
            type(a) not in (float, int)
            or type(b) not in (float, int)
            or not math.isfinite(a)
            or not math.isfinite(b)
            or not 0 <= a <= 50
            or not -50 <= b <= 50
        ):
            raise ValueError("Invalid Platt parameters")
        return _sigmoid(a * _logit(p) + b)
    if method != "isotonic_pava_v1":
        raise ValueError("Unknown calibration method")
    xs, ys = artifact.get("x"), artifact.get("y")
    if (
        not isinstance(xs, list)
        or not isinstance(ys, list)
        or not xs
        or len(xs) != len(ys)
        or len(xs) > 10000
    ):
        raise ValueError("Invalid isotonic knot arrays")
    _pairs(xs, [0] * len(xs))
    _pairs(ys, [0] * len(ys))
    if any(a >= b for a, b in zip(xs, xs[1:])) or any(a > b for a, b in zip(ys, ys[1:])):
        raise ValueError("Isotonic knots must be strictly ordered with monotone outputs")
    right = bisect_right(xs, p)
    if right == 0:
        return float(ys[0])
    if right == len(xs):
        return float(ys[-1])
    left = right - 1
    weight = (p - xs[left]) / (xs[right] - xs[left])
    return ys[left] * (1 - weight) + ys[right] * weight


def calibration_report(probabilities, outcomes, regimes=None):
    """Reliability-diagram data and losses, with explicitly descriptive intervals."""
    pairs = _pairs(probabilities, outcomes)
    if regimes is not None and len(regimes) != len(pairs):
        raise ValueError("Regime and probability counts must match")
    result = score([p for p, _ in pairs], [y for _, y in pairs])
    result["reliability_diagram"] = result["calibration"]
    result["by_volatility_regime"] = {}
    if regimes is not None:
        for regime in sorted(
            {str(value) if value is not None else "unavailable" for value in regimes}
        ):
            selected = [
                pair
                for pair, value in zip(pairs, regimes)
                if (str(value) if value is not None else "unavailable") == regime
            ]
            result["by_volatility_regime"][regime] = score(
                [p for p, _ in selected], [y for _, y in selected]
            )
    result["uncertainty_note"] = UNCERTAINTY_NOTE
    result["edge_status"] = "edge unverified"
    return result


def chronological_calibration(
    rows, *, minimum_fit=60, minimum_selection=30, minimum_test=30, asof=None
):
    """Fit/select on past labeled forecasts, then evaluate untouched future rows.

    Input rows require ``probability``, ``outcome``, ``timestamp``, ``closes_at``
    and ``label_available_at``. Optional ``data_cutoff`` and
    ``model_trained_through`` must not exceed their forecast timestamp. Rows
    belong to distinct contracts (``ticker``), when provided. The latest 20%
    remains untouched until selection is final. Late labels at each boundary
    are purged. No artifact is activated, even if its test losses improve.
    """
    for count in (minimum_fit, minimum_selection, minimum_test):
        if type(count) is not int or count < 1:
            raise ValueError("Minimum split counts must be positive integers")
    cutoff = timestamp(asof) if isinstance(asof, str) else asof or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ValueError("As-of timestamp must include a timezone")
    checked = []
    tickers = set()
    last_timestamp = None
    for source in rows:
        p, y = _pairs([source["probability"]], [source["outcome"]])[0]
        generated = timestamp(source["timestamp"])
        closed = timestamp(source["closes_at"])
        labeled = timestamp(source["label_available_at"])
        if not generated < closed <= labeled <= cutoff:
            raise ValueError(
                "Forecast, settlement and observed label timestamps are inconsistent or in the future"
            )
        if last_timestamp is not None and generated <= last_timestamp:
            raise ValueError("Calibration rows must be strictly chronological")
        last_timestamp = generated
        for field in ("data_cutoff", "model_trained_through"):
            if source.get(field) is not None and timestamp(source[field]) > generated:
                raise ValueError("Future information in forecast inputs")
        ticker = source.get("ticker")
        if ticker is not None:
            if ticker in tickers:
                raise ValueError("One pre-outcome forecast per contract is required")
            tickers.add(ticker)
        checked.append(
            {**source, "probability": p, "outcome": y, "_time": generated, "_label": labeled}
        )
    report = {
        "status": "INSUFFICIENT_CHRONOLOGICAL_LABELS",
        "samples": len(checked),
        "required_minimum": minimum_fit + minimum_selection + minimum_test,
        "promotion_allowed": False,
        "active_model_changed": False,
        "edge_status": "edge unverified",
        "method": "Chronological 60/20/20 calibration-fit / calibration-selection / untouched-test; late labels purged at boundaries",
        "label_provenance": "Caller must verify official labels and genuinely recorded pre-outcome forecasts; no historical predictions are fabricated here",
        "uncertainty_note": UNCERTAINTY_NOTE,
    }
    a, b = int(len(checked) * 0.6), int(len(checked) * 0.8)
    fit, selection, test = checked[:a], checked[a:b], checked[b:]
    if selection:
        fit = [row for row in fit if row["_label"] < selection[0]["_time"]]
    if test:
        selection = [row for row in selection if row["_label"] < test[0]["_time"]]
    report["split_samples"] = {"fit": len(fit), "selection": len(selection), "test": len(test)}
    if len(fit) < minimum_fit or len(selection) < minimum_selection or len(test) < minimum_test:
        return report
    if len({row["outcome"] for row in fit}) < 2:
        report["status"] = "CALIBRATION_FIT_HAS_ONE_CLASS"
        return report
    artifacts = [
        {"method": "identity_v1", "samples": len(fit)},
        fit_platt([row["probability"] for row in fit], [row["outcome"] for row in fit]),
        fit_isotonic([row["probability"] for row in fit], [row["outcome"] for row in fit]),
    ]
    selection_metrics = {}
    for artifact in artifacts:
        selection_metrics[artifact["method"]] = calibration_report(
            [apply_calibration(artifact, row["probability"]) for row in selection],
            [row["outcome"] for row in selection],
            [row.get("regime") for row in selection],
        )
    # Identity wins exact ties. Test outcomes are never used to select a method.
    selected = min(
        artifacts,
        key=lambda artifact: (
            selection_metrics[artifact["method"]]["log_loss"],
            selection_metrics[artifact["method"]]["brier"],
            artifact["method"] != "identity_v1",
        ),
    )
    report.update(
        status="SHADOW_CALIBRATION_EVALUATED",
        selected_before_test=selected["method"],
        calibration_artifact=selected,
        fit_labels_through=max(row["label_available_at"] for row in fit),
        selection_labels_through=max(row["label_available_at"] for row in selection),
        first_test_prediction=test[0]["timestamp"],
        validation=selection_metrics,
        test={
            artifact["method"]: calibration_report(
                [apply_calibration(artifact, row["probability"]) for row in test],
                [row["outcome"] for row in test],
                [row.get("regime") for row in test],
            )
            for artifact in artifacts
        },
    )
    return report


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _feature(row, name):
    if name == "probability":
        return row.get(name)
    return row.get("features", row).get(name)


def drift_report(reference_rows, current_rows, *, feature_names=None, bins=10, minimum_samples=30):
    """Reference-only quantile PSI, missingness and range coverage diagnostics.

    PSI thresholds are operational heuristics, not significance tests or proof
    that accuracy changed. No label information or model activation is used.
    """
    if (
        type(bins) is not int
        or not 2 <= bins <= 50
        or type(minimum_samples) is not int
        or minimum_samples < 2
    ):
        raise ValueError("Invalid drift configuration")
    reference_rows, current_rows = list(reference_rows), list(current_rows)
    if feature_names is None:
        names = set()
        for row in reference_rows:
            names.update(row.get("features", row))
            if "probability" in row:
                names.add("probability")
        feature_names = sorted(names)
    result = {
        "status": "INSUFFICIENT_DRIFT_SAMPLES",
        "reference_samples": len(reference_rows),
        "current_samples": len(current_rows),
        "features": {},
        "note": "Reference-only quantile population stability index; 0.1/0.25 watch/warning cutoffs are heuristics, not statistical significance. Distribution drift does not establish loss of skill or a trading edge. Caller must supply comparable fresh source/unit windows.",
        "automatic_model_action": False,
    }
    if len(reference_rows) < minimum_samples or len(current_rows) < minimum_samples:
        return result
    worst = 0.0
    missing_warning = False
    for name in feature_names:
        reference = sorted(
            float(value) for row in reference_rows if _number(value := _feature(row, name))
        )
        current = sorted(
            float(value) for row in current_rows if _number(value := _feature(row, name))
        )
        item = {
            "reference_available": len(reference),
            "current_available": len(current),
            "reference_missing_fraction": 1 - len(reference) / len(reference_rows),
            "current_missing_fraction": 1 - len(current) / len(current_rows),
            "psi": None,
            "status": "UNAVAILABLE",
        }
        item["missingness_change"] = (
            item["current_missing_fraction"] - item["reference_missing_fraction"]
        )
        missing_warning |= item["missingness_change"] > 0.2
        if len(reference) < minimum_samples or len(current) < minimum_samples:
            result["features"][name] = item
            continue
        low, high = reference[0], reference[-1]
        tolerance = max(1e-12, max(abs(low), abs(high), 1) * 1e-9)
        if low == high:
            edges = [low - tolerance, high + tolerance]
        else:
            edges = [low - tolerance]
            edges.extend(
                reference[min(len(reference) - 1, int(i * len(reference) / bins))]
                for i in range(1, bins)
            )
            edges.append(high + tolerance)
            edges = sorted(set(edges))
        counts = [[0] * (len(edges) + 1), [0] * (len(edges) + 1)]
        for values, bucket_counts in zip((reference, current), counts):
            for value in values:
                bucket_counts[bisect_right(edges, value)] += 1
        # Jeffreys-style smoothing gives finite values for genuinely empty bins.
        distributions = [
            [(n + 0.5) / (len(values) + 0.5 * len(bucket_counts)) for n in bucket_counts]
            for values, bucket_counts in zip((reference, current), counts)
        ]
        psi = sum((q - p) * math.log(q / p) for p, q in zip(*distributions))
        worst = max(worst, psi)
        item.update(
            psi=psi,
            status="WARNING" if psi >= 0.25 else "WATCH" if psi >= 0.1 else "STABLE",
            reference_range=[low, high],
            current_in_reference_range_fraction=sum(
                low - tolerance <= value <= high + tolerance for value in current
            )
            / len(current),
            bin_edges=edges,
            reference_distribution=distributions[0],
            current_distribution=distributions[1],
        )
        result["features"][name] = item
    result["status"] = (
        "DRIFT_WARNING"
        if worst >= 0.25 or missing_warning
        else "DRIFT_WATCH"
        if worst >= 0.1
        else "NO_LARGE_DISTRIBUTION_CHANGE"
    )
    if not any(item["psi"] is not None for item in result["features"].values()):
        result["status"] = "INSUFFICIENT_FEATURE_COVERAGE"
    result["maximum_psi"] = worst
    return result

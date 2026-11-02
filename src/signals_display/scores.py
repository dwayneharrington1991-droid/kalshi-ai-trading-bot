"""Probability metrics with explicit counts, not reliability guarantees."""
import math


def wilson_interval(correct, samples, z=1.96):
    """Descriptive binomial interval; serial dependence is not accounted for."""
    if not samples:
        return None
    p = correct / samples
    scale = 1 + z * z / samples
    center = (p + z * z / (2 * samples)) / scale
    half = z * math.sqrt(p * (1 - p) / samples + z * z / (4 * samples * samples)) / scale
    return [max(0, center - half), min(1, center + half)]


def _mean_interval(values, *, cap=None):
    if len(values) < 2:
        return None
    center = sum(values) / len(values)
    half = 1.96 * math.sqrt(
        sum((x - center) ** 2 for x in values) / (len(values) - 1) / len(values)
    )
    return [max(0, center - half), min(cap, center + half) if cap else center + half]


def score(probabilities, outcomes):
    if len(probabilities) != len(outcomes):
        raise ValueError("Probability and outcome counts must match")
    if any(not math.isfinite(float(p)) or not 0 <= float(p) <= 1 for p in probabilities):
        raise ValueError("Probabilities must be finite and within [0,1]")
    if any(y not in (0, 1) for y in outcomes):
        raise ValueError("Outcomes must be confirmed binary labels")
    n = len(outcomes)
    if not n:
        return {
            "samples": 0,
            "accuracy": None,
            "accuracy_interval": None,
            "directional_accuracy": None,
            "directional_accuracy_interval": None,
            "directional_samples": 0,
            "unclear_samples": 0,
            "brier": None,
            "brier_approximate_interval": None,
            "log_loss": None,
            "log_loss_approximate_interval": None,
            "calibration": [],
            "expected_calibration_error": None,
            "uncertainty_note": "No scored observations. Intervals do not establish live calibration or profitability.",
        }
    raw_pairs = [(float(p), int(y)) for p, y in zip(probabilities, outcomes)]
    pairs = [(max(1e-9, min(1 - 1e-9, float(p))), int(y)) for p, y in zip(probabilities, outcomes)]
    bins = []
    for k in range(10):
        group = [(p, y) for p, y in pairs if min(9, int(p * 10)) == k]
        bins.append(
            {
                "low": k / 10,
                "high": (k + 1) / 10,
                "samples": len(group),
                "mean_probability": sum(p for p, _ in group) / len(group) if group else None,
                "outcome_rate": sum(y for _, y in group) / len(group) if group else None,
                "outcome_rate_interval": wilson_interval(sum(y for _, y in group), len(group)),
            }
        )
    correct = sum((p >= 0.5) == bool(y) for p, y in raw_pairs)
    directional = [(p, y) for p, y in raw_pairs if abs(p - 0.5) > 1e-12]
    directional_correct = sum((p > 0.5) == bool(y) for p, y in directional)
    brier = [(p - y) ** 2 for p, y in raw_pairs]
    losses = [-y * math.log(p) - (1 - y) * math.log(1 - p) for p, y in pairs]
    return {
        "samples": n,
        "accuracy": correct / n,
        "accuracy_interval": wilson_interval(correct, n),
        "directional_accuracy": directional_correct / len(directional) if directional else None,
        "directional_accuracy_interval": wilson_interval(directional_correct, len(directional)),
        "directional_samples": len(directional),
        "unclear_samples": n - len(directional),
        "brier": sum(brier) / n,
        "brier_approximate_interval": _mean_interval(brier, cap=1),
        "log_loss": sum(losses) / n,
        "log_loss_approximate_interval": _mean_interval(losses),
        "calibration": bins,
        "expected_calibration_error": sum(
            b["samples"] / n * abs(b["mean_probability"] - b["outcome_rate"])
            for b in bins
            if b["samples"]
        ),
        "uncertainty_note": "Wilson binomial accuracy/rate intervals and normal approximate mean-loss intervals are descriptive only. Correlated contracts and model selection can invalidate nominal coverage; probability ranges are not validated confidence.",
    }


def horizon(seconds):
    return (
        ">10m"
        if seconds > 600
        else "5-10m"
        if seconds > 300
        else "2-5m"
        if seconds > 120
        else "<=2m"
    )

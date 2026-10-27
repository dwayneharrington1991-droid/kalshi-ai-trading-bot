"""Probability metrics with explicit counts, not reliability guarantees."""
import math


def score(probabilities, outcomes):
    n = len(outcomes)
    if not n:
        return {"samples": 0, "accuracy": None, "brier": None, "log_loss": None, "calibration": []}
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
            }
        )
    return {
        "samples": n,
        "accuracy": sum((p >= 0.5) == bool(y) for p, y in pairs) / n,
        "brier": sum((p - y) ** 2 for p, y in pairs) / n,
        "log_loss": -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in pairs) / n,
        "calibration": bins,
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

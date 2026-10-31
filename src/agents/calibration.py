"""Calibration analysis for settled shadow predictions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    average_probability: float | None
    observed_yes_rate: float | None
    calibration_gap: float | None
    status: str
    confidence_interval: Tuple[float, float] | None


def calibration_report(
    observations: Iterable[Tuple[float, int]], *, bucket_count: int = 10,
    minimum_samples: int = 20,
) -> List[CalibrationBucket]:
    if bucket_count < 2:
        raise ValueError("bucket_count must be at least two")
    buckets: List[list[Tuple[float, int]]] = [[] for _ in range(bucket_count)]
    for probability, outcome in observations:
        if not math.isfinite(probability) or not 0 <= probability <= 1 or outcome not in (0, 1):
            raise ValueError("invalid calibration observation")
        index = min(bucket_count - 1, int(probability * bucket_count))
        buckets[index].append((probability, outcome))
    report = []
    for index, values in enumerate(buckets):
        lower, upper = index / bucket_count, (index + 1) / bucket_count
        if not values:
            report.append(CalibrationBucket(lower, upper, 0, None, None, None, "NO_DATA", None))
            continue
        average = sum(v[0] for v in values) / len(values)
        observed = sum(v[1] for v in values) / len(values)
        gap = observed - average
        if len(values) < minimum_samples:
            status = "INSUFFICIENT_SAMPLE"
        elif abs(gap) <= 0.05:
            status = "CALIBRATED"
        elif gap < 0:
            status = "OVERCONFIDENT"
        else:
            status = "UNDERCONFIDENT"
        error = 1.96 * math.sqrt(max(observed * (1 - observed), 0) / len(values))
        interval = (max(0.0, observed - error), min(1.0, observed + error))
        report.append(CalibrationBucket(lower, upper, len(values), average, observed, gap, status, interval))
    return report


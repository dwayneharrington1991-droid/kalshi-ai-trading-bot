"""Offline-only performance summaries and bounded weight recommendations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Tuple


@dataclass(frozen=True)
class WeightRecommendation:
    weights: Dict[str, float]
    applied: bool
    reason: str


def recommend_weights(
    scores: Mapping[str, Tuple[int, float]], current: Mapping[str, float], *,
    minimum_samples: int = 100, maximum_change: float = 0.05,
    minimum_weight: float = 0.05, maximum_weight: float = 0.40,
    auto_apply: bool = False,
) -> WeightRecommendation:
    """Return a report only. ``auto_apply`` is deliberately ignored and never applied."""
    if not current or any(weight <= 0 for weight in current.values()):
        raise ValueError("current weights must be positive")
    if any(name not in scores or scores[name][0] < minimum_samples for name in current):
        return WeightRecommendation(dict(current), False, "INSUFFICIENT_SAMPLES")
    quality = {name: max(0.0, 1.0 - scores[name][1]) for name in current}
    total = sum(quality.values())
    if total <= 0:
        return WeightRecommendation(dict(current), False, "NO_MEASURABLE_QUALITY")
    proposed = {}
    for name, old in current.items():
        target = quality[name] / total
        bounded = max(old - maximum_change, min(old + maximum_change, target))
        proposed[name] = max(minimum_weight, min(maximum_weight, bounded))
    normalization = sum(proposed.values())
    proposed = {name: value / normalization for name, value in sorted(proposed.items())}
    return WeightRecommendation(proposed, False, "OFFLINE_RECOMMENDATION_ONLY")


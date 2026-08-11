from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ConsistencyFinding:
    relationship: str
    observed_probability: float
    expected_probability: float
    deviation: float
    requires_review: bool
    reason: str


def check_exclusive_outcomes(
    probabilities: Iterable[float], *, tolerance: float = 0.03
) -> ConsistencyFinding:
    """Check a complete mutually-exclusive outcome set without claiming arbitrage."""
    values = tuple(probabilities)
    if not values or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1
        for value in values
    ):
        raise ValueError("exclusive outcome probabilities must be complete and numeric")
    total = float(sum(values))
    deviation = abs(total - 1.0)
    return ConsistencyFinding(
        "mutually_exclusive", total, 1.0, deviation, deviation > tolerance,
        "relative-value inconsistency; execution and payoff lock are not established"
        if deviation > tolerance else "probabilities are within consistency tolerance",
    )


def check_complements(yes_probability: float, no_probability: float, *, tolerance: float = 0.02) -> ConsistencyFinding:
    """Check complementary prices; this is diagnostic, not an arbitrage assertion."""
    return check_exclusive_outcomes((yes_probability, no_probability), tolerance=tolerance)

"""Prediction and simulated-decision metrics."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Iterable, List, Mapping, Sequence, Tuple


def brier_score(values: Iterable[Tuple[float, int]]) -> float:
    rows = list(values)
    if not rows:
        raise ValueError("observations are required")
    _validate_observations(rows)
    return sum((probability - outcome) ** 2 for probability, outcome in rows) / len(rows)


def log_loss(values: Iterable[Tuple[float, int]], epsilon: float = 1e-15) -> float:
    rows = list(values)
    if not rows:
        raise ValueError("observations are required")
    _validate_observations(rows)
    if not math.isfinite(epsilon) or not 0 < epsilon < .5:
        raise ValueError("epsilon must be finite and between zero and one half")
    return -sum(
        outcome * math.log(min(1 - epsilon, max(epsilon, probability)))
        + (1 - outcome) * math.log(min(1 - epsilon, max(epsilon, 1 - probability)))
        for probability, outcome in rows
    ) / len(rows)


def _validate_observations(rows: Sequence[Tuple[float, int]]) -> None:
    for probability, outcome in rows:
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError("probability must be numeric")
        if not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1:
            raise ValueError("probability must be finite and between zero and one")
        if outcome not in (0, 1) or isinstance(outcome, bool):
            raise ValueError("outcome must be binary")


def maximum_drawdown(pnls: Sequence[float]) -> float:
    equity = peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def summarize(predictions: Sequence[Mapping[str, float | int | str]]) -> dict:
    settled = [(float(row["probability_yes"]), int(row["outcome"])) for row in predictions]
    pnls = [float(row.get("pnl", 0)) for row in predictions if row.get("action") != "ABSTAIN"]
    trades = len(pnls)
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    directional_rows = [row for row in predictions if "market_probability" in row]
    disagreement_rows = [row for row in predictions if isinstance(row.get("agent_probabilities"), (list, tuple))]
    return {
        "sample_count": len(settled), "brier_score": brier_score(settled),
        "log_loss": log_loss(settled),
        "mean_absolute_probability_error": sum(abs(p - y) for p, y in settled) / len(settled),
        "accuracy": sum((p >= .5) == bool(y) for p, y in settled) / len(settled),
        "coverage_rate": trades / len(settled), "abstention_rate": 1 - trades / len(settled),
        "win_rate": len(wins) / trades if trades else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "simulated_pnl": sum(pnls), "maximum_drawdown": maximum_drawdown(pnls),
        "simulated_expected_value": sum(float(row.get("expected_value", 0)) for row in predictions),
        "directional_agreement": (
            sum((float(row["probability_yes"]) >= .5) == (float(row["market_probability"]) >= .5) for row in directional_rows) / len(directional_rows)
            if directional_rows else None
        ),
        "agent_disagreement": (
            sum(statistics.pstdev(float(v) for v in row["agent_probabilities"]) for row in disagreement_rows) / len(disagreement_rows)
            if disagreement_rows else None
        ),
    }


def grouped_summaries(
    predictions: Sequence[Mapping[str, float | int | str]], field: str,
) -> dict:
    """Group metrics by category, horizon, liquidity, spread, side, or recommendation."""
    groups = defaultdict(list)
    for row in predictions:
        groups[str(row.get(field, "UNKNOWN"))].append(row)
    return {name: summarize(rows) for name, rows in sorted(groups.items())}

"""Chronological, deterministic replay with no exchange client dependency."""

from __future__ import annotations

import random
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List

from .costs import CostModel
from .metrics import summarize


@dataclass(frozen=True)
class ReplaySnapshot:
    snapshot_id: str
    market_id: str
    timestamp: str
    settlement_timestamp: str
    probability_yes: float
    market_probability: float
    spread: float
    liquidity: float
    outcome: int
    recommendation: str


class ReplayEngine:
    def __init__(self, costs: CostModel, seed: int = 0):
        self.costs = costs
        self.seed = seed

    def run(self, snapshots: Iterable[ReplaySnapshot], quantity: float = 1.0) -> dict:
        rows = list(snapshots)
        if not rows:
            raise ValueError("historical snapshots are required")
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be finite and positive")
        if len({row.snapshot_id for row in rows}) != len(rows):
            raise ValueError("duplicate historical snapshot")
        random.Random(self.seed)  # Explicit deterministic seed for future stochastic assumptions.
        evaluated: List[Dict[str, Any]] = []
        previous = None
        for row in rows:
            for name, value in (
                ("probability_yes", row.probability_yes),
                ("market_probability", row.market_probability),
            ):
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"{name} must be finite and between zero and one")
            if (
                not math.isfinite(row.spread) or row.spread < 0
                or not math.isfinite(row.liquidity) or row.liquidity < 0
                or row.outcome not in (0, 1)
            ):
                raise ValueError("invalid spread, liquidity, or outcome")
            prediction_time = datetime.fromisoformat(row.timestamp)
            settlement_time = datetime.fromisoformat(row.settlement_timestamp)
            if settlement_time <= prediction_time:
                raise ValueError("look-ahead or invalid settlement timestamp")
            if previous and prediction_time < previous:
                raise ValueError("snapshots must be chronological")
            previous = prediction_time
            action = row.recommendation
            pnl = 0.0
            if action in {"BUY_YES", "BUY_NO"} and row.liquidity > 0:
                side_probability = row.market_probability if action == "BUY_YES" else 1 - row.market_probability
                price = self.costs.entry_price(side_probability, row.spread)
                filled = min(quantity, row.liquidity) * self.costs.fill_fraction
                win = row.outcome == (1 if action == "BUY_YES" else 0)
                gross = filled * ((1 - price) if win else -price)
                pnl = gross - filled * price * self.costs.fee_rate
            else:
                action = "ABSTAIN"
            evaluated.append({
                "probability_yes": row.probability_yes, "outcome": row.outcome,
                "action": action, "pnl": pnl, "market_id": row.market_id,
            })
        return {
            "mode": "SIMULATED_SHADOW", "profitability_evidence": False,
            "execution_assumptions": self.costs.__dict__, "seed": self.seed,
            "rows": evaluated, "metrics": summarize(evaluated),
        }

    @staticmethod
    def time_split(
        snapshots: Iterable[ReplaySnapshot], train_end: str, validation_end: str,
    ) -> dict:
        """Create non-overlapping chronological train/validation/test partitions."""
        train_boundary = datetime.fromisoformat(train_end)
        validation_boundary = datetime.fromisoformat(validation_end)
        if validation_boundary <= train_boundary:
            raise ValueError("validation boundary must follow train boundary")
        result = {"train": [], "validation": [], "test": []}
        for row in sorted(snapshots, key=lambda item: (item.timestamp, item.snapshot_id)):
            timestamp = datetime.fromisoformat(row.timestamp)
            target = "train" if timestamp < train_boundary else "validation" if timestamp < validation_boundary else "test"
            result[target].append(row)
        return result

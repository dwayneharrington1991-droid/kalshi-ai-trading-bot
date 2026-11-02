from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from src.learning.performance_tracker import PerformanceTracker


@dataclass
class StrategyScore:
    strategy: str
    score: float
    sample_size: int
    win_rate: float
    total_pnl: float
    average_pnl: float
    profit_factor: float | None
    position_multiplier: float


class StrategyRanker:
    def __init__(
        self,
        database_path: str = "data/trade_memory.db",
        minimum_sample_size: int = 10,
    ) -> None:
        self.tracker = PerformanceTracker(database_path)
        self.minimum_sample_size = minimum_sample_size

    def rank(self) -> list[dict[str, Any]]:
        ranked: list[StrategyScore] = []

        for result in self.tracker.by_strategy():
            sample_size = int(result["total_trades"])
            win_rate = float(result["win_rate"])
            total_pnl = float(result["total_pnl"])
            average_pnl = float(result["average_pnl"])
            profit_factor = result["profit_factor"]

            sample_confidence = min(
                1.0,
                sample_size / max(self.minimum_sample_size, 1),
            )

            profit_factor_score = (
                min(float(profit_factor), 3.0) / 3.0
                if profit_factor is not None
                else (1.0 if total_pnl > 0 else 0.0)
            )

            pnl_score = max(-1.0, min(1.0, average_pnl))
            normalized_pnl = (pnl_score + 1.0) / 2.0

            raw_score = (
                0.45 * win_rate
                + 0.35 * profit_factor_score
                + 0.20 * normalized_pnl
            )

            score = raw_score * sample_confidence

            if sample_size < self.minimum_sample_size:
                multiplier = 0.50
            elif score >= 0.75:
                multiplier = 1.15
            elif score >= 0.60:
                multiplier = 1.00
            elif score >= 0.45:
                multiplier = 0.75
            else:
                multiplier = 0.50

            ranked.append(
                StrategyScore(
                    strategy=str(result["strategy"]),
                    score=round(score, 4),
                    sample_size=sample_size,
                    win_rate=round(win_rate, 4),
                    total_pnl=round(total_pnl, 4),
                    average_pnl=round(average_pnl, 4),
                    profit_factor=(
                        round(float(profit_factor), 4)
                        if profit_factor is not None
                        else None
                    ),
                    position_multiplier=multiplier,
                )
            )

        ranked.sort(key=lambda item: item.score, reverse=True)
        return [asdict(item) for item in ranked]

    def multiplier_for(self, strategy: str) -> float:
        for result in self.rank():
            if result["strategy"] == strategy:
                return float(result["position_multiplier"])

        return 0.50

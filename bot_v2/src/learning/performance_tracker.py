from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class PerformanceSummary:
    total_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float
    total_pnl: float
    average_pnl: float
    average_win: float
    average_loss: float
    profit_factor: float | None


class PerformanceTracker:
    def __init__(self, database_path: str = "data/trade_memory.db") -> None:
        self.database_path = Path(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def overall_summary(self) -> PerformanceSummary:
        if not self.database_path.exists():
            return PerformanceSummary(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None)

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT realized_pnl
                FROM trade_memory
                WHERE status = 'closed'
                  AND realized_pnl IS NOT NULL
                """
            ).fetchall()

        values = [float(row["realized_pnl"]) for row in rows]
        return self._summarize(values)

    def by_strategy(self) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            return []

        with self._connect() as connection:
            strategies = connection.execute(
                """
                SELECT DISTINCT strategy
                FROM trade_memory
                WHERE status = 'closed'
                  AND realized_pnl IS NOT NULL
                ORDER BY strategy
                """
            ).fetchall()

            results: list[dict[str, Any]] = []

            for strategy_row in strategies:
                strategy = str(strategy_row["strategy"])

                rows = connection.execute(
                    """
                    SELECT realized_pnl
                    FROM trade_memory
                    WHERE strategy = ?
                      AND status = 'closed'
                      AND realized_pnl IS NOT NULL
                    """,
                    (strategy,),
                ).fetchall()

                summary = self._summarize(
                    [float(row["realized_pnl"]) for row in rows]
                )

                result = asdict(summary)
                result["strategy"] = strategy
                results.append(result)

        return results

    @staticmethod
    def _summarize(values: list[float]) -> PerformanceSummary:
        total = len(values)
        wins = [value for value in values if value > 0]
        losses = [value for value in values if value < 0]
        breakeven = len([value for value in values if value == 0])

        total_pnl = sum(values)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))

        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else None
        )

        return PerformanceSummary(
            total_trades=total,
            wins=len(wins),
            losses=len(losses),
            breakeven=breakeven,
            win_rate=(len(wins) / total) if total else 0.0,
            total_pnl=total_pnl,
            average_pnl=(total_pnl / total) if total else 0.0,
            average_win=(gross_profit / len(wins)) if wins else 0.0,
            average_loss=(sum(losses) / len(losses)) if losses else 0.0,
            profit_factor=profit_factor,
        )

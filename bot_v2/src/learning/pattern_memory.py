from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class PatternStats:
    strategy: str
    side: str
    category: str
    confidence_bucket: str
    edge_bucket: str
    sample_size: int
    wins: int
    win_rate: float
    average_pnl: float
    total_pnl: float


class PatternMemory:
    def __init__(self, database_path: str = "data/trade_memory.db") -> None:
        self.database_path = Path(database_path)

    @staticmethod
    def confidence_bucket(value: float | None) -> str:
        if value is None:
            return "unknown"

        normalized = value * 100 if value <= 1 else value
        lower = int(normalized // 10) * 10
        upper = lower + 10
        return f"{lower}-{upper}"

    @staticmethod
    def edge_bucket(value: float | None) -> str:
        if value is None:
            return "unknown"

        normalized = value * 100 if abs(value) <= 1 else value

        if normalized < 5:
            return "<5"
        if normalized < 10:
            return "5-10"
        if normalized < 20:
            return "10-20"
        return "20+"

    def analyze(self, minimum_samples: int = 1) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            return []

        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row

            rows = connection.execute(
                """
                SELECT
                    strategy,
                    side,
                    COALESCE(category, 'unknown') AS category,
                    confidence,
                    edge,
                    realized_pnl
                FROM trade_memory
                WHERE status = 'closed'
                  AND realized_pnl IS NOT NULL
                """
            ).fetchall()

        grouped: dict[tuple[str, str, str, str, str], list[float]] = {}

        for row in rows:
            key = (
                str(row["strategy"]),
                str(row["side"]),
                str(row["category"]),
                self.confidence_bucket(row["confidence"]),
                self.edge_bucket(row["edge"]),
            )

            grouped.setdefault(key, []).append(float(row["realized_pnl"]))

        results: list[PatternStats] = []

        for key, pnl_values in grouped.items():
            if len(pnl_values) < minimum_samples:
                continue

            wins = len([value for value in pnl_values if value > 0])

            results.append(
                PatternStats(
                    strategy=key[0],
                    side=key[1],
                    category=key[2],
                    confidence_bucket=key[3],
                    edge_bucket=key[4],
                    sample_size=len(pnl_values),
                    wins=wins,
                    win_rate=wins / len(pnl_values),
                    average_pnl=sum(pnl_values) / len(pnl_values),
                    total_pnl=sum(pnl_values),
                )
            )

        results.sort(
            key=lambda item: (
                item.sample_size,
                item.average_pnl,
            ),
            reverse=True,
        )

        return [asdict(item) for item in results]

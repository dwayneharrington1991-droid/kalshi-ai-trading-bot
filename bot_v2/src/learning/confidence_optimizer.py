from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class ConfidenceBucket:
    bucket: str
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    average_pnl: float
    total_pnl: float
    recommended_adjustment: float
    eligible_for_use: bool


class ConfidenceOptimizer:
    def __init__(
        self,
        database_path: str = "data/trade_memory.db",
        minimum_samples: int = 20,
    ) -> None:
        self.database_path = Path(database_path)
        self.minimum_samples = minimum_samples

    @staticmethod
    def _normalize_confidence(value: float) -> float:
        return value * 100 if value <= 1 else value

    @classmethod
    def _bucket(cls, value: float) -> str:
        confidence = cls._normalize_confidence(value)
        lower = max(0, min(90, int(confidence // 10) * 10))
        return f"{lower}-{lower + 10}"

    def analyze(self) -> list[dict[str, Any]]:
        if not self.database_path.exists():
            return []

        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT confidence, realized_pnl
                FROM trade_memory
                WHERE status = 'closed'
                  AND confidence IS NOT NULL
                  AND realized_pnl IS NOT NULL
                """
            ).fetchall()

        grouped: dict[str, list[float]] = {}

        for row in rows:
            bucket = self._bucket(float(row["confidence"]))
            grouped.setdefault(bucket, []).append(float(row["realized_pnl"]))

        results: list[ConfidenceBucket] = []

        for bucket, values in grouped.items():
            sample_size = len(values)
            wins = len([value for value in values if value > 0])
            losses = len([value for value in values if value < 0])
            win_rate = wins / sample_size if sample_size else 0.0
            average_pnl = sum(values) / sample_size if sample_size else 0.0
            eligible = sample_size >= self.minimum_samples

            if not eligible:
                adjustment = 1.0
            elif win_rate >= 0.70 and average_pnl > 0:
                adjustment = 1.10
            elif win_rate >= 0.58 and average_pnl > 0:
                adjustment = 1.00
            elif win_rate >= 0.50 and average_pnl >= 0:
                adjustment = 0.85
            else:
                adjustment = 0.60

            results.append(
                ConfidenceBucket(
                    bucket=bucket,
                    sample_size=sample_size,
                    wins=wins,
                    losses=losses,
                    win_rate=round(win_rate, 4),
                    average_pnl=round(average_pnl, 4),
                    total_pnl=round(sum(values), 4),
                    recommended_adjustment=adjustment,
                    eligible_for_use=eligible,
                )
            )

        results.sort(key=lambda item: int(item.bucket.split("-")[0]))
        return [asdict(item) for item in results]

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


BUCKETS = ((.55, .60), (.60, .65), (.65, .70), (.70, .80), (.80, .90), (.90, 1.01))


@dataclass(frozen=True)
class CalibrationBucket:
    label: str
    predictions: int
    win_rate: float | None
    brier_score: float | None
    calibration_error: float | None


class SignalCalibrationRepository:
    """Persistent shadow-only prediction/outcome tracking."""

    def __init__(self, path: str | Path):
        self.path = str(path)

    def migrate(self) -> None:
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute("PRAGMA busy_timeout=30000")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS signal_forecasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    probability_yes REAL NOT NULL CHECK(probability_yes BETWEEN 0 AND 1),
                    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                    expected_ev_dollars REAL NOT NULL,
                    provider_contributions TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    outcome INTEGER CHECK(outcome IN (0,1)),
                    realized_pnl REAL,
                    settled_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_forecast_market_time
                ON signal_forecasts(market_id, created_at);
                COMMIT;
            """)

    def record(self, *, market_id: str, category: str, probability_yes: float,
               confidence: float, expected_ev_dollars: float,
               provider_contributions: dict, created_at: str) -> int:
        payload = json.dumps(provider_contributions, sort_keys=True, separators=(",", ":"))
        with sqlite3.connect(self.path, timeout=30) as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO signal_forecasts
                (market_id, category, probability_yes, confidence, expected_ev_dollars,
                 provider_contributions, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (market_id, category, probability_yes, confidence,
                  expected_ev_dollars, payload, created_at))
            if cursor.lastrowid:
                return int(cursor.lastrowid)
            row = db.execute(
                "SELECT id FROM signal_forecasts WHERE market_id=? AND created_at=?",
                (market_id, created_at),
            ).fetchone()
            return int(row[0])

    def settle(self, market_id: str, outcome: int, realized_pnl: float,
               settled_at: str) -> int:
        if isinstance(outcome, bool) or outcome not in (0, 1):
            raise ValueError("outcome must be binary")
        with sqlite3.connect(self.path, timeout=30) as db:
            cursor = db.execute("""
                UPDATE signal_forecasts SET outcome=?, realized_pnl=?, settled_at=?
                WHERE market_id=? AND outcome IS NULL
            """, (outcome, realized_pnl, settled_at, market_id))
            return cursor.rowcount

    def report(self) -> list[CalibrationBucket]:
        with sqlite3.connect(self.path, timeout=30) as db:
            rows = db.execute(
                "SELECT probability_yes, outcome FROM signal_forecasts WHERE outcome IS NOT NULL"
            ).fetchall()
        result = []
        for low, high in BUCKETS:
            values = [(p, o) for p, o in rows if low <= p < high]
            label = f"{int(low*100)}-{('100' if high > 1 else int(high*100))}%"
            if not values:
                result.append(CalibrationBucket(label, 0, None, None, None))
                continue
            wins = sum(outcome for _, outcome in values) / len(values)
            mean_p = sum(probability for probability, _ in values) / len(values)
            brier = sum((probability - outcome) ** 2 for probability, outcome in values) / len(values)
            result.append(CalibrationBucket(label, len(values), wins, brier, abs(mean_p - wins)))
        return result

    def provider_category_report(self) -> list[dict]:
        """Return settled calibration by provider/category for reliability updates.

        This intentionally reports evidence only. It never changes trading risk or
        provider weights automatically.
        """
        with sqlite3.connect(self.path, timeout=30) as db:
            rows = db.execute("""
                SELECT category, probability_yes, outcome, provider_contributions
                FROM signal_forecasts WHERE outcome IS NOT NULL
            """).fetchall()
        groups: dict[tuple[str, str], list[tuple[float, int]]] = {}
        for category, probability, outcome, payload in rows:
            try:
                providers = json.loads(payload)
            except (TypeError, ValueError):
                continue
            if not isinstance(providers, dict):
                continue
            for provider in providers:
                groups.setdefault((str(category), str(provider)), []).append(
                    (float(probability), int(outcome))
                )
        report = []
        for (category, provider), values in sorted(groups.items()):
            report.append({
                "category": category,
                "provider": provider,
                "samples": len(values),
                "brier_score": sum((p - o) ** 2 for p, o in values) / len(values),
                "win_rate": sum(o for _, o in values) / len(values),
            })
        return report

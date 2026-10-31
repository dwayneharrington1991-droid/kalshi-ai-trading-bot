from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TradeMemoryRecord:
    market_id: str
    strategy: str
    side: str
    entry_price: float
    quantity: int
    confidence: float | None = None
    edge: float | None = None
    category: str | None = None
    reason: str | None = None
    status: str = "open"
    exit_price: float | None = None
    realized_pnl: float | None = None
    metadata: dict[str, Any] | None = None
    opened_at: str | None = None
    closed_at: str | None = None


class TradeMemory:
    def __init__(self, database_path: str = "data/trade_memory.db") -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    confidence REAL,
                    edge REAL,
                    category TEXT,
                    reason TEXT,
                    status TEXT NOT NULL,
                    exit_price REAL,
                    realized_pnl REAL,
                    metadata_json TEXT,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_memory_market
                ON trade_memory(market_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_trade_memory_strategy
                ON trade_memory(strategy)
                """
            )

    def record_open(self, record: TradeMemoryRecord) -> int:
        opened_at = record.opened_at or datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(record.metadata or {}, sort_keys=True)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO trade_memory (
                    market_id,
                    strategy,
                    side,
                    entry_price,
                    quantity,
                    confidence,
                    edge,
                    category,
                    reason,
                    status,
                    exit_price,
                    realized_pnl,
                    metadata_json,
                    opened_at,
                    closed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.market_id,
                    record.strategy,
                    record.side,
                    record.entry_price,
                    record.quantity,
                    record.confidence,
                    record.edge,
                    record.category,
                    record.reason,
                    record.status,
                    record.exit_price,
                    record.realized_pnl,
                    metadata_json,
                    opened_at,
                    record.closed_at,
                ),
            )
            return int(cursor.lastrowid)

    def record_close(
        self,
        trade_id: int,
        exit_price: float,
        realized_pnl: float,
        status: str = "closed",
    ) -> None:
        closed_at = datetime.now(timezone.utc).isoformat()

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE trade_memory
                SET status = ?,
                    exit_price = ?,
                    realized_pnl = ?,
                    closed_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    exit_price,
                    realized_pnl,
                    closed_at,
                    trade_id,
                ),
            )

    def get_trade(self, trade_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_memory WHERE id = ?",
                (trade_id,),
            ).fetchone()

        if row is None:
            return None

        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        return result

    def list_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM trade_memory
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        trades: list[dict[str, Any]] = []

        for row in rows:
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
            trades.append(result)

        return trades

    def export_record(self, record: TradeMemoryRecord) -> dict[str, Any]:
        return asdict(record)

"""Persistent trade-decision intelligence for analysis and calibration.

This module is intentionally passive: it records what the trading system decided
and what ultimately happened, but it does not alter order selection or execution.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiosqlite


@dataclass(slots=True)
class TradeDecisionRecord:
    market_id: str
    side: str
    strategy: str
    signal_timestamp: datetime
    proposed_price: Optional[float] = None
    proposed_quantity: Optional[int] = None
    edge: Optional[float] = None
    confidence: Optional[float] = None
    predicted_probability: Optional[float] = None
    market_probability: Optional[float] = None
    rationale: Optional[str] = None
    model_version: Optional[str] = None
    agent_scores: Dict[str, float] = field(default_factory=dict)
    market_context: Dict[str, Any] = field(default_factory=dict)
    risk_context: Dict[str, Any] = field(default_factory=dict)
    live_mode: bool = False
    decision_id: Optional[int] = None


class TradeIntelligenceStore:
    """SQLite-backed signal-to-outcome audit trail."""

    def __init__(self, db_path: str = "trading_system.db") -> None:
        self.db_path = db_path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_intelligence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    signal_timestamp TEXT NOT NULL,
                    proposed_price REAL,
                    proposed_quantity INTEGER,
                    edge REAL,
                    confidence REAL,
                    predicted_probability REAL,
                    market_probability REAL,
                    rationale TEXT,
                    model_version TEXT,
                    agent_scores_json TEXT NOT NULL DEFAULT '{}',
                    market_context_json TEXT NOT NULL DEFAULT '{}',
                    risk_context_json TEXT NOT NULL DEFAULT '{}',
                    live_mode INTEGER NOT NULL DEFAULT 0,
                    execution_status TEXT NOT NULL DEFAULT 'proposed',
                    order_id TEXT,
                    executed_price REAL,
                    executed_quantity INTEGER,
                    execution_timestamp TEXT,
                    exit_price REAL,
                    exit_timestamp TEXT,
                    exit_reason TEXT,
                    realized_pnl REAL,
                    outcome INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ti_market ON trade_intelligence(market_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ti_strategy ON trade_intelligence(strategy)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ti_status ON trade_intelligence(execution_status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ti_signal_time ON trade_intelligence(signal_timestamp)"
            )
            await db.commit()

    async def record_decision(self, record: TradeDecisionRecord) -> int:
        await self.initialize()
        payload = asdict(record)
        payload["signal_timestamp"] = self._iso(record.signal_timestamp)
        payload["agent_scores_json"] = json.dumps(record.agent_scores, sort_keys=True)
        payload["market_context_json"] = json.dumps(record.market_context, sort_keys=True, default=str)
        payload["risk_context_json"] = json.dumps(record.risk_context, sort_keys=True, default=str)
        payload["live_mode"] = int(record.live_mode)

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO trade_intelligence (
                    market_id, side, strategy, signal_timestamp,
                    proposed_price, proposed_quantity, edge, confidence,
                    predicted_probability, market_probability, rationale,
                    model_version, agent_scores_json, market_context_json,
                    risk_context_json, live_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["market_id"], payload["side"], payload["strategy"],
                    payload["signal_timestamp"], payload["proposed_price"],
                    payload["proposed_quantity"], payload["edge"], payload["confidence"],
                    payload["predicted_probability"], payload["market_probability"],
                    payload["rationale"], payload["model_version"],
                    payload["agent_scores_json"], payload["market_context_json"],
                    payload["risk_context_json"], payload["live_mode"],
                ),
            )
            await db.commit()
            decision_id = int(cursor.lastrowid)
            record.decision_id = decision_id
            return decision_id

    async def mark_execution(
        self,
        decision_id: int,
        *,
        status: str,
        order_id: Optional[str] = None,
        executed_price: Optional[float] = None,
        executed_quantity: Optional[int] = None,
        error_message: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        await self.initialize()
        ts = self._iso(timestamp or datetime.now(timezone.utc))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE trade_intelligence
                SET execution_status = ?, order_id = ?, executed_price = ?,
                    executed_quantity = ?, execution_timestamp = ?, error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, order_id, executed_price, executed_quantity, ts, error_message, decision_id),
            )
            await db.commit()

    async def mark_exit(
        self,
        decision_id: int,
        *,
        exit_price: float,
        realized_pnl: float,
        exit_reason: str,
        outcome: Optional[bool] = None,
        timestamp: Optional[datetime] = None,
    ) -> None:
        await self.initialize()
        ts = self._iso(timestamp or datetime.now(timezone.utc))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE trade_intelligence
                SET execution_status = 'closed', exit_price = ?, exit_timestamp = ?,
                    exit_reason = ?, realized_pnl = ?, outcome = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (exit_price, ts, exit_reason, realized_pnl,
                 None if outcome is None else int(outcome), decision_id),
            )
            await db.commit()

    async def strategy_summary(self) -> list[dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT strategy,
                       COUNT(*) AS decisions,
                       SUM(CASE WHEN execution_status IN ('placed','filled','closed') THEN 1 ELSE 0 END) AS executed,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN realized_pnl <= 0 AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END) AS losses,
                       COALESCE(SUM(realized_pnl), 0) AS total_pnl,
                       AVG(confidence) AS avg_confidence,
                       AVG(edge) AS avg_edge
                FROM trade_intelligence
                GROUP BY strategy
                ORDER BY total_pnl DESC, decisions DESC
                """
            )
            return [dict(row) for row in await cursor.fetchall()]

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

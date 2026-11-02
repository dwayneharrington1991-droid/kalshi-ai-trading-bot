"""Transactional persistence for multi-agent shadow analyses."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiosqlite

from .models import AnalysisRunResult


class AgentRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def store_analysis(
        self, result: AnalysisRunResult, *, environment: str, cycle_id: Optional[str] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        outputs = sorted(result.outputs, key=lambda item: item.agent_name)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            try:
                await db.execute("BEGIN IMMEDIATE")
                existing = await (await db.execute(
                    "SELECT id FROM agent_analysis_runs WHERE request_id = ?",
                    (result.request.request_id,),
                )).fetchone()
                if existing:
                    await db.rollback()
                    return int(existing[0])
                cursor = await db.execute("""
                    INSERT INTO agent_analysis_runs (
                        request_id, market_id, cycle_id, environment, mode, started_at,
                        completed_at, status, agent_count, successful_agent_count,
                        failed_agent_count, total_latency, total_cost, configuration_version
                    ) VALUES (?, ?, ?, ?, 'shadow', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    result.request.request_id, result.request.market_id, cycle_id,
                    environment, result.request.analysis_timestamp, now, result.status,
                    len(outputs), sum(o.success for o in outputs), sum(not o.success for o in outputs),
                    sum(o.latency_seconds for o in outputs), sum(o.estimated_cost or 0 for o in outputs),
                    result.request.configuration_version,
                ))
                run_id = int(cursor.lastrowid)
                for output in outputs:
                    await db.execute("""
                        INSERT INTO agent_outputs (
                            analysis_run_id, agent_name, agent_version, model_name,
                            probability_yes, confidence, evidence_quality, risk_level, veto,
                            recommendation, success, fallback_used, latency, token_count,
                            estimated_cost, sanitized_output, error_category, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        run_id, output.agent_name, output.agent_version, output.model_name,
                        output.probability_yes, output.confidence,
                        1.0 if output.evidence_sufficient else 0.0,
                        output.risk_level.value if output.risk_level else None, output.veto,
                        output.recommendation.value, output.success, output.fallback_used,
                        output.latency_seconds, output.token_count, output.estimated_cost,
                        output.sanitized_json(), output.error_category, output.timestamp,
                    ))
                consensus = result.consensus
                await db.execute("""
                    INSERT INTO consensus_predictions (
                        analysis_run_id, market_id, probability_yes, probability_no,
                        process_confidence, disagreement_score, evidence_quality_score,
                        uncertainty_score, recommendation, expected_edge, risk_veto,
                        explanation, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, consensus.market_id, consensus.probability_yes,
                    consensus.probability_no, consensus.process_confidence,
                    consensus.disagreement_score, consensus.evidence_quality_score,
                    consensus.uncertainty_score, consensus.recommendation.value,
                    consensus.expected_edge, consensus.risk_veto,
                    consensus.explanation, consensus.created_at,
                ))
                single = result.request.single_model_analysis
                single_probability = single.get("probability_yes", single.get("probability"))
                single_action = single.get("action")
                await db.execute("""
                    INSERT INTO single_vs_multi_comparisons (
                        analysis_run_id, market_id, timestamp, market_price_probability,
                        single_model_probability, multi_agent_probability, single_model_action,
                        multi_agent_action, probability_difference, action_agreement, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    run_id, result.request.market_id, result.request.analysis_timestamp,
                    result.request.yes_price, single_probability, consensus.probability_yes,
                    single_action, consensus.recommendation.value,
                    (consensus.probability_yes - single_probability) if isinstance(single_probability, (int, float)) else None,
                    (str(single_action).upper() == consensus.recommendation.value) if single_action else None,
                    now,
                ))
                await db.execute("""
                    INSERT INTO prediction_outcomes (
                        market_id, analysis_run_id, prediction_timestamp,
                        predicted_probability_yes, market_probability_at_prediction,
                        outcome_status, eligible_at_prediction, category,
                        time_horizon_seconds, model_version
                    ) VALUES (?, ?, ?, ?, ?, 'unresolved', 1, ?, ?, ?)
                """, (
                    result.request.market_id, run_id, result.request.analysis_timestamp,
                    consensus.probability_yes, result.request.yes_price,
                    result.request.category, result.request.time_remaining_seconds,
                    result.request.configuration_version,
                ))
                await db.commit()
                return run_id
            except Exception:
                await db.rollback()
                raise

    async def resolve_outcome(
        self, market_id: str, status: str, actual_outcome: Optional[int],
        settlement_timestamp: Optional[str],
    ) -> int:
        if status not in {"settled", "voided", "canceled", "ambiguous"}:
            raise ValueError("unsupported outcome status")
        if status == "settled" and actual_outcome not in {0, 1}:
            raise ValueError("settled outcome must be 0 or 1")
        if not isinstance(market_id, str) or not market_id.strip():
            raise ValueError("market_id is required")
        if not isinstance(settlement_timestamp, str) or not settlement_timestamp.strip():
            raise ValueError("settlement_timestamp is required")
        try:
            parsed_timestamp = datetime.fromisoformat(settlement_timestamp)
        except ValueError:
            raise ValueError("settlement_timestamp must be ISO-8601") from None
        if parsed_timestamp.tzinfo is None:
            raise ValueError("settlement_timestamp must include a timezone")
        if status != "settled" and actual_outcome is not None:
            raise ValueError("non-settled outcomes cannot have a binary result")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute("""
                    UPDATE prediction_outcomes
                    SET actual_outcome = ?, outcome_status = ?, settlement_timestamp = ?,
                        settled_at = ?
                    WHERE market_id = ? AND outcome_status = 'unresolved'
                """, (
                    actual_outcome, status, settlement_timestamp,
                    datetime.now(timezone.utc).isoformat(), market_id,
                ))
                await db.commit()
                return cursor.rowcount
            except Exception:
                await db.rollback()
                raise

    async def export_rows(self) -> Dict[str, Any]:
        tables = ("agent_analysis_runs", "agent_outputs", "consensus_predictions", "single_vs_multi_comparisons", "prediction_outcomes")
        result: Dict[str, Any] = {}
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            for table in tables:
                rows = await (await db.execute(f"SELECT * FROM {table} ORDER BY id")).fetchall()
                result[table] = [dict(row) for row in rows]
        return result

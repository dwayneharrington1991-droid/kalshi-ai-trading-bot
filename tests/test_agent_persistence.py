import asyncio

import aiosqlite
import pytest

from src.agents.orchestrator import MultiAgentOrchestrator
from src.agents.repository import AgentRepository
from src.agents.outcomes import OutcomeResolutionService
from src.utils.database import DatabaseManager
from tests.test_multi_agent_shadow import executors, request


pytestmark = pytest.mark.asyncio


async def test_fresh_migration_and_idempotent_restart(tmp_path):
    path = str(tmp_path / "agent.db")
    await DatabaseManager(path).initialize()
    result = await MultiAgentOrchestrator(executors()).analyze(request())
    repository = AgentRepository(path)
    first = await repository.store_analysis(result, environment="test")
    second = await repository.store_analysis(result, environment="test")
    assert first == second
    rows = await repository.export_rows()
    assert len(rows["agent_analysis_runs"]) == 1
    assert len(rows["agent_outputs"]) == 5
    assert len(rows["consensus_predictions"]) == 1
    assert len(rows["prediction_outcomes"]) == 1


async def test_concurrent_duplicate_request_and_trading_tables_unchanged(tmp_path):
    path = str(tmp_path / "concurrent.db")
    await DatabaseManager(path).initialize()
    result = await MultiAgentOrchestrator(executors()).analyze(request())
    repository = AgentRepository(path)
    before = {}
    async with aiosqlite.connect(path) as db:
        for table in ("orders", "positions", "order_fills"):
            before[table] = (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0]
    identifiers = await asyncio.gather(*[
        repository.store_analysis(result, environment="test") for _ in range(4)
    ])
    assert len(set(identifiers)) == 1
    async with aiosqlite.connect(path) as db:
        for table, count in before.items():
            assert (await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone())[0] == count


async def test_upgrade_preserves_existing_rows(tmp_path):
    path = str(tmp_path / "upgrade.db")
    manager = DatabaseManager(path)
    await manager.initialize()
    async with aiosqlite.connect(path) as db:
        await db.execute("INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            "LEGACY", "Legacy", .4, .6, 1, 2000000000, "test", "active", "2026-01-01", 0,
        ))
        await db.execute("DELETE FROM schema_migrations WHERE version = 4")
        for table in ("agent_outputs", "consensus_predictions", "single_vs_multi_comparisons", "prediction_outcomes", "agent_performance", "calibration_buckets", "backtest_runs", "agent_analysis_runs"):
            await db.execute(f"DROP TABLE {table}")
        await db.commit()
    await manager.initialize()
    async with aiosqlite.connect(path) as db:
        assert (await (await db.execute("SELECT title FROM markets WHERE market_id='LEGACY'")).fetchone())[0] == "Legacy"
        assert (await (await db.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=4")).fetchone())[0] == 1


async def test_migration_rollback(tmp_path, monkeypatch):
    path = str(tmp_path / "rollback.db")
    original = DatabaseManager._migration_004_multi_agent_shadow

    async def fail(self, db):
        await db.execute("CREATE TABLE rollback_probe (id INTEGER)")
        raise RuntimeError("fixture failure")

    monkeypatch.setattr(DatabaseManager, "_migration_004_multi_agent_shadow", fail)
    with pytest.raises(RuntimeError):
        await DatabaseManager(path).initialize()
    async with aiosqlite.connect(path) as db:
        tables = {row[0] for row in await (await db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert "rollback_probe" not in tables


async def test_outcome_resolution_is_idempotent_and_preserves_unresolved(tmp_path):
    path = str(tmp_path / "outcomes.db")
    await DatabaseManager(path).initialize()
    repository = AgentRepository(path)
    result = await MultiAgentOrchestrator(executors()).analyze(request())
    await repository.store_analysis(result, environment="test")
    service = OutcomeResolutionService(repository)
    first = await service.resolve([{"market_id": "MKT", "status": "settled", "actual_outcome": 1, "settlement_timestamp": "2026-01-02T00:00:00+00:00"}])
    second = await service.resolve([{"market_id": "MKT", "status": "settled", "actual_outcome": 0, "settlement_timestamp": "2026-01-03T00:00:00+00:00"}])
    assert first["updated"] == 1 and second["updated"] == 0
    rows = await repository.export_rows()
    assert rows["prediction_outcomes"][0]["actual_outcome"] == 1


async def test_unresolved_voided_and_invalid_settlements(tmp_path):
    path = str(tmp_path / "statuses.db")
    await DatabaseManager(path).initialize()
    repository = AgentRepository(path)
    result = await MultiAgentOrchestrator(executors()).analyze(request())
    await repository.store_analysis(result, environment="test")
    service = OutcomeResolutionService(repository)
    assert (await service.resolve([{"market_id": "MKT", "status": "unresolved"}]))["updated"] == 0
    rows = await repository.export_rows()
    assert rows["prediction_outcomes"][0]["outcome_status"] == "unresolved"
    with pytest.raises(ValueError, match="timestamp"):
        await repository.resolve_outcome("MKT", "voided", None, "not-a-time")
    changed = await service.resolve([{
        "market_id": "MKT", "status": "voided",
        "settlement_timestamp": "2026-01-02T00:00:00+00:00",
    }])
    assert changed["updated"] == 1
    rows = await repository.export_rows()
    assert rows["prediction_outcomes"][0]["outcome_status"] == "voided"
    assert rows["prediction_outcomes"][0]["actual_outcome"] is None

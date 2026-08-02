import aiosqlite
import pytest

from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio
EXPECTED_TABLES = {
    "schema_migrations", "orders", "order_fills", "order_state_events",
    "reconciliation_runs", "reconciliation_alerts",
    "position_projection_baselines", "position_fill_projections",
}


async def table_names(db_path):
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
        return {row[0] for row in rows}


async def test_fresh_database_applies_versioned_migrations(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    assert EXPECTED_TABLES <= await table_names(db_path)
    async with aiosqlite.connect(db_path) as db:
        versions = await (await db.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )).fetchall()
    assert versions == [(1,), (2,), (3,)]


async def test_migrations_are_idempotent(tmp_path):
    db_path = str(tmp_path / "repeat.db")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    await manager.initialize()
    async with aiosqlite.connect(db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM schema_migrations")).fetchone())[0]
    assert count == 3


async def test_legacy_data_is_preserved(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL,
                side TEXT NOT NULL, entry_price REAL NOT NULL, quantity INTEGER NOT NULL,
                timestamp TEXT NOT NULL, rationale TEXT, confidence REAL,
                live BOOLEAN NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
                UNIQUE(market_id, side))
        """)
        await db.execute("""
            CREATE TABLE trade_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL, side TEXT NOT NULL,
                entry_price REAL NOT NULL, exit_price REAL NOT NULL, quantity INTEGER NOT NULL,
                pnl REAL NOT NULL, entry_timestamp TEXT NOT NULL, exit_timestamp TEXT NOT NULL,
                rationale TEXT)
        """)
        await db.execute("""
            INSERT INTO positions
                (market_id, side, entry_price, quantity, timestamp, rationale, confidence, live, status)
            VALUES ('LEGACY', 'YES', 0.4, 7, '2026-01-01T00:00:00', 'QUICK FLIP: old', 0.8, 1, 'open')
        """)
        await db.commit()
    await DatabaseManager(db_path).initialize()
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("""
            SELECT market_id, side, entry_price, quantity, strategy, filled_quantity,
                   reconciliation_status, legacy_unreconciled
            FROM positions WHERE market_id = 'LEGACY'
        """)).fetchone()
    assert row == ("LEGACY", "YES", 0.4, 7, "quick_flip_scalping", None, None, 0)


async def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rollback.db")
    manager = DatabaseManager(db_path)

    async def fail(_db):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(manager, "_migration_002_order_reconciliation", fail)
    with pytest.raises(RuntimeError):
        await manager.initialize()
    assert await table_names(db_path) == set()


async def test_failed_migration_preserves_existing_database(tmp_path, monkeypatch):
    db_path = str(tmp_path / "legacy-rollback.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        await db.execute("INSERT INTO sentinel VALUES ('preserve-me')")
        await db.commit()
    manager = DatabaseManager(db_path)

    async def fail(_db):
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(manager, "_migration_002_order_reconciliation", fail)
    with pytest.raises(RuntimeError):
        await manager.initialize()
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT value FROM sentinel")).fetchone()
        migration_table = await (await db.execute(
            "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'"
        )).fetchone()
    assert row == ("preserve-me",)
    assert migration_table is None

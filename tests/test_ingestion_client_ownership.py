import asyncio

import pytest

from src.jobs.ingest import run_ingestion
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


class ReadClient:
    def __init__(self):
        self.close_calls = 0

    async def get_market(self, ticker):
        return {}

    async def close(self):
        self.close_calls += 1


async def test_injected_ingestion_client_is_not_closed(tmp_path):
    manager = DatabaseManager(str(tmp_path / "injected.db"))
    await manager.initialize()
    client = ReadClient()
    await run_ingestion(manager, asyncio.Queue(), market_ticker="MKT", kalshi_client=client)
    assert client.close_calls == 0


async def test_normal_ingestion_still_constructs_and_closes_client(tmp_path, monkeypatch):
    manager = DatabaseManager(str(tmp_path / "owned.db"))
    await manager.initialize()
    client = ReadClient()
    monkeypatch.setattr("src.jobs.ingest.KalshiClient", lambda: client)
    await run_ingestion(manager, asyncio.Queue(), market_ticker="MKT")
    assert client.close_calls == 1

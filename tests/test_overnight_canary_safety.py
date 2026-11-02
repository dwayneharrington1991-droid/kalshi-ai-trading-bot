from datetime import datetime, timezone

import pytest

from src.orders.execution_service import (
    ExecutionSafetyConfig,
    ExecutionSafetyError,
    OrderIntent,
    VerifiedExecutionService,
)
from src.orders.repository import OrderRepository
from src.utils.database import DatabaseManager, Market, Position


class NoNetworkClient:
    environment = "demo"

    def __getattr__(self, name):
        raise AssertionError(f"unexpected exchange access: {name}")


class NoReconciliation:
    project_positions = False


async def setup_service(tmp_path, **overrides):
    path = tmp_path / "canary.db"
    manager = DatabaseManager(str(path))
    await manager.initialize()
    market = Market(
        market_id="CANARY-MKT", title="Canary", yes_price=.40, no_price=.60,
        volume=1000, expiration_ts=2_000_000_000, category="test", status="active",
        last_updated=datetime.now(timezone.utc),
    )
    await manager.upsert_markets([market])
    values = dict(overnight_canary_enabled=True)
    values.update(overrides)
    service = VerifiedExecutionService(
        OrderRepository(str(path)), NoNetworkClient(), NoReconciliation(),
        ExecutionSafetyConfig(**values),
    )
    intent = OrderIntent("CANARY-MKT", "YES", "buy", 2, .40, "limit", 1, "demo")
    return manager, service, intent


@pytest.mark.asyncio
async def test_canary_allows_order_within_all_caps(tmp_path):
    _, service, intent = await setup_service(tmp_path, canary_max_market_risk=5.0)
    await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_canary_market_risk_is_a_cap_not_a_required_size(tmp_path):
    _, service, intent = await setup_service(tmp_path, canary_max_market_risk=5.0)
    assert intent.price * intent.quantity < 5.0
    await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_canary_blocks_per_market_risk(tmp_path):
    _, service, intent = await setup_service(tmp_path, canary_max_market_risk=.50)
    with pytest.raises(ExecutionSafetyError, match="per-market"):
        await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_canary_blocks_total_risk_and_position_limit(tmp_path):
    manager, service, intent = await setup_service(
        tmp_path, canary_max_total_risk=1.0, canary_max_positions=1
    )
    await manager.add_position(Position(
        market_id="OTHER", side="YES", entry_price=.50, quantity=1,
        timestamp=datetime.now(timezone.utc), live=True, status="open",
    ))
    with pytest.raises(ExecutionSafetyError, match="total capital"):
        await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_canary_fails_closed_when_market_data_is_missing(tmp_path):
    _, service, intent = await setup_service(tmp_path)
    intent = OrderIntent("MISSING", "YES", "buy", 1, .40, "limit", 1, "demo")
    with pytest.raises(ExecutionSafetyError, match="market data"):
        await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_canary_is_disabled_by_default(tmp_path):
    _, service, intent = await setup_service(tmp_path, overnight_canary_enabled=False)
    await service._canary_gateway(intent)


@pytest.mark.asyncio
async def test_historical_position_is_never_destructively_replaced(tmp_path):
    manager, _, _ = await setup_service(tmp_path)
    first = Position(
        market_id="HISTORY", side="YES", entry_price=.25, quantity=2,
        timestamp=datetime.now(timezone.utc), live=True, status="closed",
    )
    first_id = await manager.add_position(first)
    replacement = Position(
        market_id="HISTORY", side="YES", entry_price=.75, quantity=9,
        timestamp=datetime.now(timezone.utc), status="pending",
    )
    assert await manager.add_position(replacement) is None
    stored = await manager.get_position_by_id(first_id)
    assert stored.entry_price == .25
    assert stored.quantity == 2
    assert stored.status == "closed"

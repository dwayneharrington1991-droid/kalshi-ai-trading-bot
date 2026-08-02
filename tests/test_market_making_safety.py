from datetime import datetime
import inspect

import pytest

from src.strategies.market_making import (
    AdvancedMarketMaker, LimitOrder, MarketMakingOpportunity,
)
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


class NoSubmissionClient:
    environment = "demo"

    def __init__(self):
        self.place_calls = 0

    async def place_order(self, **kwargs):
        self.place_calls += 1
        raise AssertionError("market making must not call place_order")


@pytest.fixture
async def maker(tmp_path):
    manager = DatabaseManager(str(tmp_path / "market-making.db"))
    await manager.initialize()
    client = NoSubmissionClient()
    return AdvancedMarketMaker(manager, client, None), client


def opportunity():
    return MarketMakingOpportunity(
        market_id="MKT", market_title="Test", current_yes_price=.5,
        current_no_price=.5, ai_predicted_prob=.5, ai_confidence=.8,
        optimal_yes_bid=.45, optimal_yes_ask=.55,
        optimal_no_bid=.45, optimal_no_ask=.55,
        yes_spread_profit=1, no_spread_profit=1, total_expected_profit=2,
        inventory_risk=.1, volatility_estimate=.1,
        optimal_yes_size=10, optimal_no_size=10,
    )


async def test_kill_switch_blocks_market_making_without_submission(maker, monkeypatch):
    strategy, client = maker
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_trading_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.authoritative_live_execution_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.order_reconciliation_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_order_submission_kill_switch", True)
    monkeypatch.setattr("src.strategies.market_making.settings.api.kalshi_environment", "demo")
    success, reason = await strategy._place_limit_order(LimitOrder("MKT", "YES", 45, 10))
    assert success is False
    assert reason == "live order submission kill switch is enabled"
    assert client.place_calls == 0


async def test_disabled_authoritative_execution_cannot_be_bypassed(maker, monkeypatch):
    strategy, client = maker
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_trading_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.authoritative_live_execution_enabled", False)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.order_reconciliation_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_order_submission_kill_switch", False)
    monkeypatch.setattr("src.strategies.market_making.settings.api.kalshi_environment", "demo")
    result = await strategy.execute_market_making_strategy([opportunity()])
    assert result["orders_placed"] == 0
    assert result["orders_blocked"] == 2
    assert result["refusal_reasons"] == ["authoritative execution is disabled"] * 2
    assert client.place_calls == 0


async def test_fully_open_gates_still_fail_closed_until_verified_integration(maker, monkeypatch):
    strategy, client = maker
    repository = __import__("src.orders.repository", fromlist=["OrderRepository"]).OrderRepository(
        strategy.db_manager.db_path
    )
    run_id = await repository.start_reconciliation_run("health")
    await repository.finish_reconciliation_run(run_id, "completed")
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_trading_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.authoritative_live_execution_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.order_reconciliation_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_order_submission_kill_switch", False)
    monkeypatch.setattr("src.strategies.market_making.settings.api.kalshi_environment", "demo")
    success, reason = await strategy._place_limit_order(LimitOrder("MKT", "YES", 45, 10))
    assert success is False
    assert reason == "market-making verified execution integration is unavailable"
    assert client.place_calls == 0


async def test_paper_mode_remains_isolated_simulation(maker, monkeypatch):
    strategy, client = maker
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_trading_enabled", False)
    order = LimitOrder("MKT", "YES", 45, 10)
    success, reason = await strategy._place_limit_order(order)
    assert success is True
    assert reason == "paper simulation"
    assert order.order_id.startswith("sim_")
    assert client.place_calls == 0


async def test_unexpected_opportunity_exception_is_blocked_and_sanitized(
    maker, monkeypatch, capsys,
):
    strategy, client = maker
    secret = "SECRET_EXCEPTION_TEXT"

    async def fail(_opportunity):
        raise RuntimeError(secret)

    monkeypatch.setattr(strategy, "_place_market_making_orders", fail)
    result = await strategy.execute_market_making_strategy([opportunity()])
    assert result["orders_placed"] == 0
    assert result["orders_blocked"] == 2
    assert secret not in capsys.readouterr().out
    assert client.place_calls == 0


async def test_duplicate_opportunities_never_become_active_when_blocked(maker, monkeypatch):
    strategy, client = maker
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_trading_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.authoritative_live_execution_enabled", False)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.order_reconciliation_enabled", True)
    monkeypatch.setattr("src.strategies.market_making.settings.trading.live_order_submission_kill_switch", True)
    monkeypatch.setattr("src.strategies.market_making.settings.api.kalshi_environment", "demo")
    result = await strategy.execute_market_making_strategy([opportunity(), opportunity()])
    assert result["orders_placed"] == 0
    assert result["orders_blocked"] == 4
    assert strategy.active_orders == {}
    assert client.place_calls == 0


async def test_market_making_source_has_no_direct_place_order_call():
    source = inspect.getsource(AdvancedMarketMaker)
    assert ".place_order(" not in source

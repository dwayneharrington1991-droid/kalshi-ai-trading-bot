"""Regression tests for bounded market-making analysis in unified cycles."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.strategies.unified_trading_system import (
    TradingSystemConfig,
    UnifiedAdvancedTradingSystem,
)


class SlowMarketMaker:
    def __init__(self):
        self.received = []
        self.cancelled = asyncio.Event()

    async def analyze_market_making_opportunities(self, markets):
        self.received = list(markets)
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


class EmptyMarketMaker:
    def __init__(self):
        self.received = []

    async def analyze_market_making_opportunities(self, markets):
        self.received = list(markets)
        return []


def _system(market_maker, *, limit=3, timeout=0.02):
    system = object.__new__(UnifiedAdvancedTradingSystem)
    system.config = TradingSystemConfig(
        market_making_scan_limit=limit,
        market_making_analysis_timeout_seconds=timeout,
    )
    system.market_maker = market_maker
    system.market_making_capital = 0.0
    system.logger = Mock()
    return system


def _markets(count):
    return [
        SimpleNamespace(market_id=f"M{index}", volume=float(index))
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_huge_market_universe_cannot_stall_parallel_cycle_analysis():
    """A stalled market-making provider must not hold up the other branch."""
    maker = SlowMarketMaker()
    system = _system(maker, limit=5, timeout=0.01)

    async def directional_branch():
        return "directional-completed"

    market_making, directional = await asyncio.wait_for(
        asyncio.gather(
            system._execute_market_making_strategy(_markets(22_840)),
            directional_branch(),
        ),
        timeout=0.2,
    )

    assert directional == "directional-completed"
    assert market_making == {'orders_placed': 0, 'expected_profit': 0.0}
    assert len(maker.received) == 5
    assert [market.market_id for market in maker.received] == [
        "M22839", "M22838", "M22837", "M22836", "M22835",
    ]
    await asyncio.wait_for(maker.cancelled.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_market_making_scan_is_bounded_to_highest_volume_markets():
    maker = EmptyMarketMaker()
    system = _system(maker, limit=3, timeout=1)

    result = await system._execute_market_making_strategy(_markets(10))

    assert result == {'orders_placed': 0, 'expected_profit': 0.0}
    assert [market.market_id for market in maker.received] == ["M9", "M8", "M7"]

import logging
import time

import pytest

from src.orders.execution_service import (
    ExecutionSafetyError, OrderIntent, VerifiedExecutionService,
)


class FreshMarketClient:
    def __init__(self, yes_ask=.70, liquidity=10):
        self.yes_ask = yes_ask
        self.liquidity = liquidity
        self.calls = []

    async def get_market(self, ticker):
        self.calls.append(("get_market", ticker))
        return {"market": {
            "yes_bid_dollars": str(self.yes_ask - .02),
            "yes_ask_dollars": str(self.yes_ask),
            "no_bid_dollars": str(1 - self.yes_ask - .02),
            "no_ask_dollars": str(1 - self.yes_ask),
        }}

    async def get_orderbook(self, ticker, depth=100):
        self.calls.append(("get_orderbook", ticker, depth))
        return {"orderbook_fp": {"no_dollars": [[str(1 - self.yes_ask), str(self.liquidity)]]}}


def service(client):
    instance = object.__new__(VerifiedExecutionService)
    instance.client = client
    instance.logger = logging.getLogger("fast-live-test")
    return instance


def intent(**overrides):
    values = dict(
        market_id="KXBTC15M", side="YES", action="buy", quantity=5,
        price=.70, order_type="limit", position_id=1, environment="production",
        strategy="portfolio_optimization", estimated_probability=.80,
        model_generated_at=time.time(), market_phase="FAST_LIVE",
    )
    values.update(overrides)
    return OrderIntent(**values)


@pytest.mark.asyncio
async def test_fast_live_fresh_quote_and_liquidity_pass():
    client = FreshMarketClient()
    await service(client)._fresh_directional_gateway(intent())
    assert [call[0] for call in client.calls] == ["get_market", "get_orderbook"]


@pytest.mark.asyncio
async def test_fast_live_quote_move_cancels_without_chasing():
    with pytest.raises(ExecutionSafetyError, match="quote moved"):
        await service(FreshMarketClient(yes_ask=.71))._fresh_directional_gateway(intent())


@pytest.mark.asyncio
async def test_fast_live_stale_model_fails_before_exchange_reads():
    client = FreshMarketClient()
    with pytest.raises(ExecutionSafetyError, match="model probability is stale"):
        await service(client)._fresh_directional_gateway(
            intent(model_generated_at=time.time() - 3600)
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_fast_live_insufficient_executable_liquidity_blocks():
    with pytest.raises(ExecutionSafetyError, match="liquidity"):
        await service(FreshMarketClient(liquidity=4))._fresh_directional_gateway(intent())

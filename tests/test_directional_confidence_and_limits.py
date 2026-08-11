from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config.settings import settings
from src.strategies.directional_policy import validate_directional_model_confidence
from src.strategies.portfolio.immediate import create_market_opportunities_from_markets
from src.utils.database import Market
from src.utils.position_limits import PositionLimitsManager


def test_low_model_confidence_remains_an_independent_safety_gate():
    accepted, reason = validate_directional_model_confidence(0.25)
    assert accepted is False
    assert reason == "model confidence 25.0% below minimum 35.0%"

    accepted, reason = validate_directional_model_confidence(0.35)
    assert accepted is True
    assert reason == "model confidence 35.0% meets minimum"


@pytest.mark.parametrize("value", [True, None, -0.1, 1.1, "0.8"])
def test_model_confidence_validation_fails_closed(value):
    accepted, _ = validate_directional_model_confidence(value)
    assert accepted is False


def test_position_limits_use_canary_maximum(monkeypatch):
    monkeypatch.setattr(settings.trading, "overnight_canary_enabled", True)
    monkeypatch.setattr(settings.trading, "overnight_canary_max_positions", 5)
    monkeypatch.setattr(settings.trading, "max_positions", 10)

    manager = PositionLimitsManager(SimpleNamespace(), SimpleNamespace())

    assert manager.max_positions == 5
    assert manager.warning_threshold == 2


def test_position_limits_use_standard_maximum_outside_canary(monkeypatch):
    monkeypatch.setattr(settings.trading, "overnight_canary_enabled", False)
    monkeypatch.setattr(settings.trading, "max_positions", 10)

    manager = PositionLimitsManager(SimpleNamespace(), SimpleNamespace())

    assert manager.max_positions == 10


@pytest.mark.asyncio
async def test_directional_pass_with_low_confidence_does_not_reach_allocation(monkeypatch):
    async def prediction(*_args, **_kwargs):
        return 0.62, 0.25

    class FakeKalshi:
        async def get_market(self, _ticker):
            return {"market": {
                "status": "active",
                "yes_bid_dollars": ".56", "yes_ask_dollars": ".57",
                "no_bid_dollars": ".42", "no_ask_dollars": ".43",
                "close_time": "2099-01-01T00:00:00Z", "title": "Fixture",
            }}

        async def get_orderbook(self, _ticker, depth=100):
            return {"orderbook_fp": {
                "no_dollars": [[".43", "100"]], "yes_dollars": [[".57", "100"]],
            }}

    monkeypatch.setattr(
        "src.strategies.portfolio.immediate._get_fast_ai_prediction", prediction
    )
    market = Market(
        "TEST", "Fixture", 0.56, 0.43, 10_000,
        datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp(),
        "Other", "active", datetime.now(), False,
    )

    opportunities = await create_market_opportunities_from_markets(
        [market], object(), FakeKalshi()
    )

    assert opportunities == []


@pytest.mark.asyncio
async def test_directional_pass_with_sufficient_confidence_reaches_allocation(monkeypatch):
    async def prediction(*_args, **_kwargs):
        return 0.62, 0.35

    class FakeKalshi:
        async def get_market(self, _ticker):
            return {"market": {
                "status": "active",
                "yes_bid_dollars": ".56", "yes_ask_dollars": ".57",
                "no_bid_dollars": ".42", "no_ask_dollars": ".43",
                "close_time": "2099-01-01T00:00:00Z", "title": "Fixture",
            }}

        async def get_orderbook(self, _ticker, depth=100):
            return {"orderbook_fp": {
                "no_dollars": [[".43", "100"]], "yes_dollars": [[".57", "100"]],
            }}

    monkeypatch.setattr(
        "src.strategies.portfolio.immediate._get_fast_ai_prediction", prediction
    )
    market = Market(
        "TEST", "Fixture", 0.56, 0.43, 10_000,
        datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp(),
        "Other", "active", datetime.now(), False,
    )

    opportunities = await create_market_opportunities_from_markets(
        [market], object(), FakeKalshi()
    )

    assert len(opportunities) == 1
    assert opportunities[0].recommended_side == "YES"
    assert opportunities[0].net_expected_return > 0

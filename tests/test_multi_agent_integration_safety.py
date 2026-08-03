from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agents.models import MarketAnalysisRequest
from src.config.settings import MultiAgentShadowConfig
from src.strategies.portfolio.models import MarketOpportunity
from src.strategies.unified_trading_system import UnifiedAdvancedTradingSystem
from src.utils.database import Market


def test_disabled_defaults_and_trading_impact_refusal(monkeypatch):
    for name in ("MULTI_AGENT_SHADOW_ENABLED", "MULTI_AGENT_CAN_AFFECT_TRADING"):
        monkeypatch.delenv(name, raising=False)
    config = MultiAgentShadowConfig()
    assert config.enabled is False
    assert config.can_affect_trading is False
    monkeypatch.setenv("MULTI_AGENT_CAN_AFFECT_TRADING", "true")
    with pytest.raises(RuntimeError, match="forbidden"):
        MultiAgentShadowConfig()


@pytest.mark.asyncio
async def test_shadow_hook_does_not_change_opportunity_or_call_execution(monkeypatch):
    captured = []

    class Service:
        async def analyze(self, request, cycle_id=None):
            captured.append(request)
            return SimpleNamespace()

    system = object.__new__(UnifiedAdvancedTradingSystem)
    system.multi_agent_shadow_service = Service()
    system.logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda *a, **k: None)
    monkeypatch.setattr("src.strategies.unified_trading_system.settings.multi_agent_shadow.max_markets_per_cycle", 1)
    market = Market(
        "MKT", "Fixture", .4, .6, 100,
        int((datetime.now() + timedelta(days=1)).timestamp()), "test", "active", datetime.now(),
    )
    opportunity = MarketOpportunity(
        "MKT", "Fixture", .7, .4, .8, .3, .1, .1, 1, 1, 0,
        .1, .05, .05, 1, 1, .01,
    )
    before = opportunity.__dict__.copy()
    await system._run_multi_agent_shadow([market], [opportunity])
    assert opportunity.__dict__ == before
    assert len(captured) == 1
    assert isinstance(captured[0], MarketAnalysisRequest)


@pytest.mark.asyncio
async def test_shadow_failure_isolated_from_trading(monkeypatch):
    class Service:
        async def analyze(self, request, cycle_id=None):
            raise RuntimeError("model unavailable")

    system = object.__new__(UnifiedAdvancedTradingSystem)
    system.multi_agent_shadow_service = Service()
    errors = []
    system.logger = SimpleNamespace(info=lambda *a, **k: None, error=lambda message, *a, **k: errors.append(message))
    market = Market(
        "MKT", "Fixture", .4, .6, 100,
        int((datetime.now() + timedelta(days=1)).timestamp()), "test", "active", datetime.now(),
    )
    opportunity = MarketOpportunity(
        "MKT", "Fixture", .7, .4, .8, .3, .1, .1, 1, 1, 0,
        .1, .05, .05, 1, 1, .01,
    )
    assert await system._run_multi_agent_shadow([market], [opportunity]) is None
    assert errors == ["MULTI_AGENT_SHADOW market=%s status=FAILED trading_impact=NONE"]


@pytest.mark.asyncio
async def test_exact_one_cycle_summary_and_one_failed_market_summary(monkeypatch):
    class Service:
        async def analyze(self, request, cycle_id=None):
            raise RuntimeError("failed")

    records = {"info": [], "error": []}
    system = object.__new__(UnifiedAdvancedTradingSystem)
    system.multi_agent_shadow_service = Service()
    system.logger = SimpleNamespace(
        info=lambda message, *args, **kwargs: records["info"].append(message),
        error=lambda message, *args, **kwargs: records["error"].append(message),
    )
    market = Market("MKT", "Fixture", .4, .6, 100, int((datetime.now() + timedelta(days=1)).timestamp()), "test", "active", datetime.now())
    opportunity = MarketOpportunity("MKT", "Fixture", .7, .4, .8, .3, .1, .1, 1, 1, 0, .1, .05, .05, 1, 1, .01)
    await system._run_multi_agent_shadow([market], [opportunity])
    assert records["error"].count("MULTI_AGENT_SHADOW market=%s status=FAILED trading_impact=NONE") == 1
    assert records["info"].count("MULTI_AGENT_SHADOW cycle analyzed=%s failed=%s trading_impact=NONE") == 1

from datetime import datetime, timedelta, timezone

import pytest

from src.jobs.kxbtc15m_shadow import run_kxbtc15m_shadow_cycle
from src.strategies.kxbtc15m import KXBTC15MSettings


def active_market():
    return {
        "ticker": "KXBTC15M-TEST", "status": "active",
        "open_time": datetime.now(timezone.utc).isoformat(),
        "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "floor_strike": 100000.0, "cap_strike": None,
        "strike_type": "greater_or_equal", "event_ticker": "KXBTC15M-EVENT",
        "rules_primary": "Official source.", "rules_secondary": "Official close rule.",
    }


class ReadOnlyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_balance(self):
        self.calls.append("balance")
        return {"balance": 1}

    async def get_markets(self, **kwargs):
        self.calls.append("markets")
        assert kwargs["series_ticker"] == "KXBTC15M"
        return {"markets": [active_market()], "cursor": None}

    async def get_series(self, ticker):
        self.calls.append("series")
        assert ticker == "KXBTC15M"
        return {"series": {"fee_type": "quadratic", "fee_multiplier": 1,
                           "settlement_sources": [{"name": "CF Benchmarks"}]}}

    async def get_orderbook(self, ticker):
        self.calls.append("orderbook")
        assert ticker == "KXBTC15M-TEST"
        return {"orderbook_fp": {"yes_dollars": [["0.14", "3"]],
                                  "no_dollars": [["0.85", "4"]]}}


@pytest.mark.asyncio
async def test_runtime_routes_active_contract_through_prices_and_fails_closed_without_ws():
    client = ReadOnlyClient()
    result = await run_kxbtc15m_shadow_cycle(
        client, strategy_settings=KXBTC15MSettings(enabled=True), max_market_risk=1,
    )
    assert client.calls == ["balance", "markets", "series", "orderbook"]
    assert result.ticker == "KXBTC15M-TEST"
    assert result.yes_bid == pytest.approx(.14)
    assert result.yes_ask == pytest.approx(.15)
    assert result.no_bid == pytest.approx(.85)
    assert result.no_ask == pytest.approx(.86)
    assert result.action == "NO_TRADE"
    assert "WebSocket order book is stale" in result.reason
    assert not result.would_submit
    assert result.hypothetical_contracts == 0


@pytest.mark.asyncio
async def test_runtime_does_not_continue_when_balance_cannot_be_verified():
    class BalanceFailureClient(ReadOnlyClient):
        async def get_balance(self):
            raise RuntimeError("unavailable")

    result = await run_kxbtc15m_shadow_cycle(
        BalanceFailureClient(), strategy_settings=KXBTC15MSettings(enabled=True), max_market_risk=1,
    )
    assert result.action == "NO_TRADE"
    assert result.reason == "balance verification failed"
    assert not result.would_submit

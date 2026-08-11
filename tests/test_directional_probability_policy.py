import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.config.settings import TradingConfig
from src.strategies.portfolio.immediate import create_market_opportunities_from_markets
from src.strategies.directional_policy import (
    classify_market_phase,
    classify_sports_phase,
    evaluate_directional_candidate,
    executable_liquidity,
    log_directional_evaluation,
)
from src.utils.database import Market


def evaluate(model, yes_bid, yes_ask, no_bid, no_ask, phase="NOT_APPLICABLE"):
    return evaluate_directional_candidate(
        market_id="TEST", predicted_yes_probability=model,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        min_probability=.65, max_preferred_probability=.90,
        min_edge=.05, fee_estimate=.01, slippage_estimate=.005,
        sports_phase=phase,
    )


def test_one_to_ten_percent_longshot_is_rejected_by_default():
    result = evaluate(.12, .03, .04, .95, .97)
    assert result.side == "YES"
    assert not result.accepted
    assert "below preferred minimum" in result.reason


def test_preferred_probability_candidate_can_qualify():
    result = evaluate(.80, .68, .70, .29, .31)
    assert result.accepted
    assert result.side == "YES"
    assert result.preferred_band
    assert result.estimated_net_return > 0


def test_fifty_five_percent_candidate_can_clear_probability_gate():
    result = evaluate_directional_candidate(
        market_id="TEST", predicted_yes_probability=.62,
        yes_bid=.54, yes_ask=.55, no_bid=.44, no_ask=.46,
        min_probability=.55, max_preferred_probability=.90,
        min_edge=.05, fee_estimate=.01, slippage_estimate=.005,
    )
    assert result.accepted
    assert result.side == "YES"
    assert result.market_implied_probability == .55
    assert result.estimated_probability == .62


def test_high_probability_without_positive_edge_is_rejected():
    result = evaluate(.80, .82, .84, .15, .17)
    assert not result.accepted
    assert result.estimated_net_return <= 0


def test_ninety_nine_percent_contract_is_not_automatically_favored():
    result = evaluate(.991, .985, .99, .005, .01)
    assert not result.accepted


def test_extreme_model_market_discrepancy_requires_additional_validation():
    result = evaluate(.95, .64, .65, .34, .35)
    assert not result.accepted
    assert "requires additional validation" in result.reason


def test_pregame_sports_does_not_require_live_marker():
    assert classify_sports_phase({}, "Sports") == "PRE_GAME"
    result = evaluate(.80, .68, .70, .29, .31, "PRE_GAME")
    assert result.accepted
    assert result.sports_phase == "PRE_GAME"


def test_live_sports_uses_same_positive_value_policy():
    assert classify_sports_phase({"in_play": True}, "Sports") == "LIVE"
    result = evaluate(.80, .82, .84, .15, .17, "LIVE")
    assert not result.accepted
    assert result.sports_phase == "LIVE"


def test_short_duration_open_market_is_fast_live_in_any_category():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expiry = (now + timedelta(minutes=15)).timestamp()
    assert classify_market_phase({}, "Crypto", expiration_ts=expiry, now=now) == "FAST_LIVE"


def test_btc_15_minute_uses_close_time_not_delayed_expected_resolution():
    now = datetime(2026, 8, 11, 3, 42, 6, tzinfo=timezone.utc)
    market = {
        "close_time": "2026-08-11T03:45:00Z",
        "expected_expiration_time": "2026-08-11T09:30:00Z",
    }
    delayed_resolution = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc).timestamp()
    assert classify_market_phase(
        market, "Crypto", expiration_ts=delayed_resolution, now=now
    ) == "FAST_LIVE"


def test_executable_liquidity_uses_opposing_bids_for_buy_ask():
    orderbook = {"orderbook_fp": {
        "yes_dollars": [["0.30", "4"]],
        "no_dollars": [["0.30", "7"], ["0.25", "3"]],
    }}
    assert executable_liquidity(orderbook, "YES", .70) == 7
    assert executable_liquidity(orderbook, "NO", .70) == 4


def test_executable_liquidity_handles_dollars_cents_and_empty_books():
    dollars = {"orderbook_fp": {
        "no_dollars": [["0.15", "12.50"], ["0.14", "99"]],
        "yes_dollars": [["0.41", "8.25"], ["0.40", "99"]],
    }}
    assert executable_liquidity(dollars, "YES", .85) == 12.5
    assert executable_liquidity(dollars, "NO", .59) == 8.25
    cents = {"orderbook": {"no": [[15, 7]], "yes": [[41, 4]]}}
    assert executable_liquidity(cents, "YES", .85) == 7
    assert executable_liquidity(cents, "NO", .59) == 4
    assert executable_liquidity({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, "YES", .85) == 0


def test_malformed_orderbook_fails_closed():
    assert executable_liquidity({"orderbook_fp": {"no_dollars": [["bad", 2]]}}, "YES", .7) == 0


def test_candidate_log_contains_required_sports_fields(caplog):
    result = evaluate(.80, .68, .70, .29, .31, "PRE_GAME")
    with caplog.at_level(logging.INFO):
        log_directional_evaluation(logging.getLogger("test"), result)
    message = caplog.text
    for field in ("sports_phase=PRE_GAME", "implied=", "estimated=", "edge=", "price=", "net_return=", "reason="):
        assert field in message


@pytest.mark.asyncio
async def test_candidate_propagates_fresh_executable_liquidity(monkeypatch, capsys):
    async def prediction(*_args, **_kwargs):
        return .80, .90

    class Client:
        async def get_market(self, _ticker):
            return {"market": {
                "yes_bid_dollars": ".68", "yes_ask_dollars": ".70",
                "no_bid_dollars": ".30", "no_ask_dollars": ".32",
                "close_time": "2099-01-01T00:00:00Z",
                "title": "Fixture",
            }}

        async def get_orderbook(self, _ticker, depth=100):
            assert depth == 100
            return {"orderbook_fp": {
                "no_dollars": [[".30", "12"]], "yes_dollars": [],
            }}

    monkeypatch.setattr(
        "src.strategies.portfolio.immediate._get_fast_ai_prediction", prediction
    )
    market = Market(
        "TEST", "Fixture", .69, .31, 1000,
        datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp(),
        "Crypto", "active", datetime.now(), False,
    )
    opportunities = await create_market_opportunities_from_markets(
        [market], object(), Client()
    )
    assert len(opportunities) == 1
    assert opportunities[0].executable_liquidity == 12
    assert opportunities[0].proposed_quantity == 7
    output = capsys.readouterr().out
    assert "liquidity=12.00 proposed_quantity=7.00" in output
    assert "structured_game_state_available=None" in output


def test_probability_band_defaults_and_canary_limits_are_preserved(monkeypatch):
    for name in (
        "MIN_PREFERRED_PROBABILITY", "MAX_PREFERRED_PROBABILITY",
        "OVERNIGHT_CANARY_MAX_TOTAL_RISK", "OVERNIGHT_CANARY_MAX_MARKET_RISK",
        "OVERNIGHT_CANARY_MAX_POSITIONS", "OVERNIGHT_CANARY_MAX_DAILY_LOSS",
        "OVERNIGHT_CANARY_MAX_REJECTIONS",
    ):
        monkeypatch.delenv(name, raising=False)
    config = TradingConfig()
    assert (config.min_preferred_probability, config.max_preferred_probability) == (.55, .90)
    assert config.overnight_canary_max_total_risk == 20
    assert config.overnight_canary_max_market_risk == 5
    assert config.overnight_canary_max_positions == 5
    assert config.overnight_canary_max_daily_loss == 5
    assert config.overnight_canary_max_rejections == 3

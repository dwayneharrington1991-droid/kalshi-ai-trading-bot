from types import SimpleNamespace
import sqlite3

import pytest

from src.strategies.sports_markets import (
    admit_event_exposure,
    classify_sports_market_type,
    deduplicate_and_cap_event_exposure,
    event_correlation_group,
)
from src.strategies.portfolio.optimizer import AdvancedPortfolioOptimizer


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        ({"market_type": "moneyline"}, "WINNER"),
        ({"title": "Minnesota -1.5 spread"}, "SPREAD"),
        ({"title": "Game total runs over 8.5"}, "GAME_TOTAL"),
        ({"title": "Baltimore team total over 3.5"}, "TEAM_TOTAL"),
        ({"title": "Player strikeouts over 6.5"}, "PROP"),
        ({"title": "First inning result"}, "OTHER"),
    ],
)
def test_all_sports_derivative_types_are_classified_without_an_allowlist(market, expected):
    assert classify_sports_market_type(market) == expected


def test_authoritative_event_ticker_is_the_correlation_group():
    winner = {"ticker": "GAME-WIN", "event_ticker": "GAME"}
    total = {"ticker": "GAME-TOTAL-8", "event_ticker": "GAME"}
    assert event_correlation_group(winner) == event_correlation_group(total) == "GAME"


def test_related_markets_are_ranked_independently_but_event_exposure_is_capped():
    winner = SimpleNamespace(
        market_id="GAME-WIN", ranking_score=.10, correlation_group="GAME",
        proposed_risk=5.0, existing_correlated_exposure=0.0, correlation_reason="",
    )
    total = SimpleNamespace(
        market_id="GAME-TOTAL", ranking_score=.20, correlation_group="GAME",
        proposed_risk=5.0, existing_correlated_exposure=0.0, correlation_reason="",
    )
    other_game = SimpleNamespace(
        market_id="OTHER-SPREAD", ranking_score=.15, correlation_group="OTHER",
        proposed_risk=5.0, existing_correlated_exposure=0.0, correlation_reason="",
    )
    selected = deduplicate_and_cap_event_exposure(
        [winner, total, other_game], max_event_risk=5.0,
    )
    assert [item.market_id for item in selected] == ["GAME-TOTAL", "OTHER-SPREAD"]


def test_existing_correlated_exposure_blocks_new_derivative():
    decision = admit_event_exposure(
        correlation_group="GAME", proposed_risk=2.0,
        existing={"GAME": 4.0}, selected={}, max_event_risk=5.0,
    )
    assert not decision.accepted
    assert decision.existing_exposure == 4.0
    assert "correlated" in decision.reason


def test_duplicate_contract_is_not_evaluated_twice():
    item = SimpleNamespace(
        market_id="GAME-TOTAL", ranking_score=.2, correlation_group="GAME",
        proposed_risk=1.0, existing_correlated_exposure=0.0, correlation_reason="",
    )
    selected = deduplicate_and_cap_event_exposure([item, item], max_event_risk=5.0)
    assert len(selected) == 1


def test_unknown_future_derivative_remains_eligible_as_other():
    assert classify_sports_market_type({"market_type": "new_nested_product"}) == "OTHER"


@pytest.mark.asyncio
async def test_existing_related_position_is_included_in_event_exposure(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE positions (market_id TEXT, open_quantity REAL, quantity REAL, "
            "entry_price REAL, live INTEGER, status TEXT)"
        )
        db.execute(
            "INSERT INTO positions VALUES ('GAME-WINNER', 4, 4, .75, 1, 'open')"
        )
    optimizer = object.__new__(AdvancedPortfolioOptimizer)
    optimizer.db_manager = SimpleNamespace(db_path=str(path))
    optimizer.logger = SimpleNamespace(error=lambda *args, **kwargs: None)
    opportunity = SimpleNamespace(correlation_group="GAME")
    exposure = await optimizer._existing_event_exposure([opportunity])
    assert exposure == {"GAME": 3.0}


@pytest.mark.asyncio
async def test_unavailable_position_ledger_fails_correlated_exposure_closed(tmp_path):
    optimizer = object.__new__(AdvancedPortfolioOptimizer)
    optimizer.db_manager = SimpleNamespace(db_path=str(tmp_path / "missing.db"))
    optimizer.logger = SimpleNamespace(error=lambda *args, **kwargs: None)
    exposure = await optimizer._existing_event_exposure([
        SimpleNamespace(correlation_group="GAME")
    ])
    assert exposure["GAME"] == 5.0

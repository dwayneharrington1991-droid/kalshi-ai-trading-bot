from datetime import datetime, timedelta, timezone

import pytest

from src.clients.kalshi_client import KalshiAPIError
from src.markets.discovery import MarketDiscovery72h


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def stamp(delta):
    return (NOW + delta).isoformat().replace("+00:00", "Z")


def market(ticker, expiry=Ellipsis):
    value = {"ticker": ticker, "title": ticker, "status": "active"}
    if expiry is not Ellipsis:
        value["expected_expiration_time"] = expiry
    return value


def event(ticker, category, markets, expiry=Ellipsis):
    value = {"event_ticker": ticker, "category": category, "markets": markets}
    if expiry is not Ellipsis:
        value["expected_expiration_time"] = expiry
    return value


class DiscoveryClient:
    def __init__(self, event_pages, milestone_pages=None):
        self.event_pages = event_pages
        self.milestone_pages = milestone_pages or [{"milestones": [], "cursor": ""}]
        self.event_calls = []
        self.milestone_calls = []

    async def get_events(self, **kwargs):
        self.event_calls.append(kwargs)
        index = 0 if kwargs.get("cursor") is None else int(kwargs["cursor"])
        return self.event_pages[index]

    async def get_milestones(self, **kwargs):
        self.milestone_calls.append(kwargs)
        index = 0 if kwargs.get("cursor") is None else int(kwargs["cursor"])
        return self.milestone_pages[index]


async def test_strict_72_hour_boundary_and_missing_expiry_fail_closed():
    client = DiscoveryClient([{
        "events": [event("EV", "Politics", [
            market("WITHIN", stamp(timedelta(hours=71, minutes=59))),
            market("EXACT", stamp(timedelta(hours=72))),
            market("LONG", stamp(timedelta(hours=72, seconds=1))),
            market("MISSING"),
            market("MALFORMED", "tomorrow-ish"),
        ])],
        "cursor": "",
        "milestones": [],
    }])
    result = await MarketDiscovery72h(client, now=NOW).discover()
    assert [item["ticker"] for item in result.markets] == ["EXACT", "WITHIN"]
    assert result.stats.markets_discovered == 5
    assert result.stats.markets_within_72h == 2
    assert result.stats.excluded_long_dated_markets == 1
    assert result.stats.excluded_missing_expiration_markets == 2


async def test_sports_and_non_sports_are_included_from_structured_metadata():
    milestone = {
        "id": "game-1",
        "category": "Sports",
        "type": "basketball_game",
        "start_date": stamp(timedelta(hours=1)),
        "end_date": stamp(timedelta(hours=4)),
        "related_event_tickers": ["GAME"],
        "primary_event_tickers": ["GAME"],
    }
    client = DiscoveryClient(
        [{
            "events": [
                event("GAME", "Other", [market("GAME-SPREAD"), market("GAME-TOTAL")]),
                event("WEATHER", "Weather", [market("WEATHER-HIGH", stamp(timedelta(hours=8)))]),
                event("FUTURE", "BrandNewCategory", [market("FUTURE-MKT", stamp(timedelta(hours=9)))]),
            ],
            "cursor": "",
            "milestones": [milestone],
        }],
        [{"milestones": [milestone], "cursor": ""}],
    )
    result = await MarketDiscovery72h(client, now=NOW).discover()
    assert {item["ticker"] for item in result.markets} == {
        "FUTURE-MKT", "GAME-SPREAD", "GAME-TOTAL", "WEATHER-HIGH"
    }
    assert result.stats.sports_markets_within_72h == 2
    assert result.stats.non_sports_markets_within_72h == 2
    sports = [item for item in result.markets if item["_is_sports_market"]]
    assert all(item["_milestone_ids"] == ["game-1"] for item in sports)
    assert client.milestone_calls[0]["category"] == "Sports"


async def test_all_pages_are_read_and_duplicate_paths_are_deduplicated():
    duplicate = market("DUP", stamp(timedelta(hours=2)))
    client = DiscoveryClient([
        {"events": [event("ONE", "Sports", [duplicate])], "cursor": "1", "milestones": []},
        {"events": [event("ONE", "Sports", [dict(duplicate)]), event(
            "TWO", "Economics", [market("OTHER", stamp(timedelta(hours=3)))]
        )], "cursor": "", "milestones": []},
    ])
    result = await MarketDiscovery72h(client, now=NOW).discover()
    assert [item["ticker"] for item in result.markets] == ["DUP", "OTHER"]
    assert len(client.event_calls) == 2
    assert client.event_calls[0]["status"] == "open"
    assert client.event_calls[0]["with_nested_markets"] is True
    assert client.event_calls[0]["with_milestones"] is True


async def test_conflicting_duplicate_expiration_is_excluded_as_ambiguous():
    client = DiscoveryClient([{
        "events": [
            event("ONE", "Sports", [market("DUP", stamp(timedelta(hours=2)))]),
            event("TWO", "Sports", [market("DUP", stamp(timedelta(hours=3)))]),
        ],
        "cursor": "",
        "milestones": [],
    }])
    result = await MarketDiscovery72h(client, now=NOW).discover()
    assert result.markets == []
    assert result.stats.excluded_missing_expiration_markets == 1


async def test_malformed_page_or_repeated_cursor_fails_closed():
    with pytest.raises(KalshiAPIError, match="Malformed events"):
        await MarketDiscovery72h(
            DiscoveryClient([{"events": "bad", "cursor": ""}]), now=NOW
        ).discover()

    client = DiscoveryClient([
        {"events": [], "cursor": "1", "milestones": []},
        {"events": [], "cursor": "1", "milestones": []},
    ])
    with pytest.raises(KalshiAPIError, match="Repeated events"):
        await MarketDiscovery72h(client, now=NOW).discover()

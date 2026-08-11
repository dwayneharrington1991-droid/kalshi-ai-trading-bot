"""Fail-closed discovery of open Kalshi markets expected to resolve within 72 hours."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.clients.kalshi_client import KalshiAPIError


@dataclass(frozen=True)
class MarketScanStats:
    markets_discovered: int = 0
    markets_within_72h: int = 0
    sports_markets_within_72h: int = 0
    non_sports_markets_within_72h: int = 0
    excluded_long_dated_markets: int = 0
    excluded_missing_expiration_markets: int = 0
    candidates_passed_to_strategy: int = 0

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class MarketDiscoveryResult:
    markets: List[Dict[str, Any]]
    stats: MarketScanStats


def _parse_rfc3339(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _linked_event_tickers(milestone: Dict[str, Any]) -> Iterable[str]:
    for field in ("primary_event_tickers", "related_event_tickers"):
        values = milestone.get(field, [])
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value:
                    yield value


def _is_sports_milestone(milestone: Dict[str, Any]) -> bool:
    category = str(milestone.get("category", "")).casefold()
    milestone_type = str(milestone.get("type", "")).casefold()
    return category in {"sports", "esports"} or any(
        marker in milestone_type
        for marker in ("game", "match", "tournament", "race")
    )


class MarketDiscovery72h:
    """Discover every open category, admitting only unambiguous <=72h markets."""

    HORIZON_HOURS = 72

    def __init__(self, client: Any, *, now: Optional[datetime] = None):
        self.client = client
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.horizon = self.now + timedelta(hours=self.HORIZON_HOURS)

    async def _pages(self, method_name: str, item_key: str, **filters):
        cursor = None
        seen_cursors = set()
        while True:
            response = await getattr(self.client, method_name)(cursor=cursor, **filters)
            if not isinstance(response, dict) or not isinstance(response.get(item_key), list):
                raise KalshiAPIError(f"Malformed {item_key} discovery response")
            if "cursor" not in response:
                raise KalshiAPIError(f"{item_key} discovery response is missing cursor")
            yield response
            cursor = response.get("cursor")
            if cursor is not None and not isinstance(cursor, str):
                raise KalshiAPIError(f"Invalid {item_key} pagination cursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                raise KalshiAPIError(f"Repeated {item_key} pagination cursor")
            seen_cursors.add(cursor)

    async def _sports_milestones(self) -> List[Dict[str, Any]]:
        milestones: Dict[str, Dict[str, Any]] = {}
        async for response in self._pages(
            "get_milestones",
            "milestones",
            limit=500,
            category="Sports",
            minimum_start_date=self.now.isoformat().replace("+00:00", "Z"),
        ):
            for milestone in response["milestones"]:
                if not isinstance(milestone, dict) or not _is_sports_milestone(milestone):
                    continue
                milestone_id = milestone.get("id")
                if isinstance(milestone_id, str) and milestone_id:
                    milestones.setdefault(milestone_id, milestone)
        return list(milestones.values())

    @staticmethod
    def _sports_event(event: Dict[str, Any], milestones: Sequence[Dict[str, Any]]) -> bool:
        category = str(event.get("category", "")).casefold()
        if category in {"sports", "esports"}:
            return True
        return any(_is_sports_milestone(milestone) for milestone in milestones)

    def _resolution_time(
        self,
        market: Dict[str, Any],
        event: Dict[str, Any],
        milestones: Sequence[Dict[str, Any]],
    ) -> Optional[datetime]:
        # A present-but-malformed preferred field is ambiguous and must not fall back.
        if "expected_expiration_time" in market:
            return _parse_rfc3339(market.get("expected_expiration_time"))
        if "expected_expiration_time" in event:
            return _parse_rfc3339(event.get("expected_expiration_time"))

        # Structured sports milestone end times are an accepted expected-resolution source.
        ends = {
            parsed
            for parsed in (_parse_rfc3339(item.get("end_date")) for item in milestones)
            if parsed is not None
        }
        if len(ends) == 1:
            return next(iter(ends))
        return None

    async def discover(self) -> MarketDiscoveryResult:
        sports_milestones = await self._sports_milestones()
        milestones_by_event: Dict[str, List[Dict[str, Any]]] = {}
        for milestone in sports_milestones:
            for ticker in _linked_event_tickers(milestone):
                milestones_by_event.setdefault(ticker, []).append(milestone)

        occurrences: Dict[str, List[tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]]] = {}
        async for response in self._pages(
            "get_events",
            "events",
            limit=200,
            status="open",
            with_nested_markets=True,
            with_milestones=True,
            min_close_ts=int(self.now.timestamp()),
        ):
            page_milestones = response.get("milestones", [])
            if not isinstance(page_milestones, list):
                raise KalshiAPIError("Malformed events milestone metadata")
            for milestone in page_milestones:
                if isinstance(milestone, dict) and _is_sports_milestone(milestone):
                    for ticker in _linked_event_tickers(milestone):
                        milestones_by_event.setdefault(ticker, []).append(milestone)
            for event in response["events"]:
                if not isinstance(event, dict):
                    raise KalshiAPIError("Malformed event in discovery response")
                event_ticker = event.get("event_ticker", event.get("ticker"))
                linked = milestones_by_event.get(str(event_ticker), [])
                markets = event.get("markets", [])
                if not isinstance(markets, list):
                    raise KalshiAPIError("Malformed nested markets in event response")
                for market in markets:
                    if not isinstance(market, dict):
                        raise KalshiAPIError("Malformed market in event response")
                    ticker = market.get("ticker")
                    if not isinstance(ticker, str) or not ticker or market.get("status") != "active":
                        continue
                    occurrences.setdefault(ticker, []).append((market, event, linked))

        admitted: List[Dict[str, Any]] = []
        sports_count = 0
        excluded_long = 0
        excluded_missing = 0
        for ticker, copies in occurrences.items():
            resolutions = [self._resolution_time(*copy) for copy in copies]
            if any(value is None for value in resolutions) or len(set(resolutions)) != 1:
                excluded_missing += 1
                continue
            resolution = resolutions[0]
            if resolution < self.now or resolution > self.horizon:
                excluded_long += 1
                continue
            market, event, linked = copies[0]
            is_sports = self._sports_event(event, linked)
            normalized = dict(market)
            normalized["category"] = event.get("category", market.get("category", "unknown"))
            normalized["expected_expiration_time"] = resolution.isoformat().replace("+00:00", "Z")
            normalized["_eligible_resolution_time"] = normalized["expected_expiration_time"]
            normalized["_is_sports_market"] = is_sports
            normalized["_milestone_ids"] = sorted({
                item["id"] for item in linked if isinstance(item.get("id"), str)
            })
            admitted.append(normalized)
            sports_count += int(is_sports)

        admitted.sort(key=lambda market: market["ticker"])
        stats = MarketScanStats(
            markets_discovered=len(occurrences),
            markets_within_72h=len(admitted),
            sports_markets_within_72h=sports_count,
            non_sports_markets_within_72h=len(admitted) - sports_count,
            excluded_long_dated_markets=excluded_long,
            excluded_missing_expiration_markets=excluded_missing,
        )
        return MarketDiscoveryResult(admitted, stats)

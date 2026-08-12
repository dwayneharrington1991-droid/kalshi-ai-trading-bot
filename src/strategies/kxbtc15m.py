"""Fail-closed, KXBTC15M-only market intelligence.

This module deliberately contains no order submission code.  It is a narrow,
testable data-and-decision layer that can only hand a verified decision to the
existing execution boundary in a later, separately enabled integration phase.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any, Iterable, Optional


KXBTC15M_SERIES = "KXBTC15M"
OPEN_STATUSES = {"open", "active"}


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def contract_close_time(market: dict[str, Any]) -> Optional[datetime]:
    """Return the authoritative trading-close timestamp, never a ticker guess."""
    for name in ("close_time", "expiration_time", "expected_expiration_time"):
        result = _parse_time(market.get(name))
        if result is not None:
            return result
    return None


def seconds_to_expiration(market: dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    close = contract_close_time(market)
    if close is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0.0, (close - current).total_seconds())


def discover_active_contract(markets: Iterable[dict[str, Any]], *, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Return the nearest still-open KXBTC15M contract, or ``None`` fail-closed."""
    current = now or datetime.now(timezone.utc)
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for market in markets:
        if not isinstance(market, dict) or market.get("series_ticker") != KXBTC15M_SERIES:
            continue
        if str(market.get("status", "")).casefold() not in OPEN_STATUSES:
            continue
        close = contract_close_time(market)
        if close is None or close <= current:
            continue
        candidates.append((close, market))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


class KXBTC15MDiscovery:
    """Paginated, series-constrained discovery with no ticker inference."""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def active_contract(self, *, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        cursor: Optional[str] = None
        seen: set[str] = set()
        markets: list[dict[str, Any]] = []
        while True:
            if cursor is not None:
                if cursor in seen:
                    raise RuntimeError("repeated KXBTC15M market pagination cursor")
                seen.add(cursor)
            response = await self.client.get_markets(
                limit=200, cursor=cursor, series_ticker=KXBTC15M_SERIES, status="open",
            )
            if not isinstance(response, dict) or not isinstance(response.get("markets"), list):
                raise RuntimeError("malformed KXBTC15M market discovery response")
            page = response["markets"]
            if not all(isinstance(item, dict) for item in page):
                raise RuntimeError("malformed KXBTC15M market row")
            markets.extend(page)
            next_cursor = response.get("cursor")
            if next_cursor is None or next_cursor == "":
                break
            if not isinstance(next_cursor, str):
                raise RuntimeError("invalid KXBTC15M market pagination cursor")
            cursor = next_cursor
        return discover_active_contract(markets, now=now)


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass
class ReconstructedOrderBook:
    """Small snapshot-plus-delta book with strict sequence integrity checks."""
    yes_bids: dict[float, float] = field(default_factory=dict)
    no_bids: dict[float, float] = field(default_factory=dict)
    sequence: Optional[int] = None
    received_at: Optional[float] = None
    valid: bool = False

    @staticmethod
    def _levels(raw: Any) -> Optional[dict[float, float]]:
        if not isinstance(raw, list):
            return None
        result: dict[float, float] = {}
        for level in raw:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                return None
            try:
                price, quantity = float(level[0]), float(level[1])
            except (TypeError, ValueError):
                return None
            if price > 1:
                price /= 100.0
            if not (0 < price < 1) or quantity < 0:
                return None
            if quantity:
                result[price] = quantity
        return result

    def apply_snapshot(self, message: dict[str, Any], *, received_at: Optional[float] = None) -> bool:
        payload = message.get("msg", message.get("orderbook", message)) if isinstance(message, dict) else None
        if not isinstance(payload, dict):
            self.valid = False
            return False
        yes = self._levels(payload.get("yes_dollars", payload.get("yes", [])))
        no = self._levels(payload.get("no_dollars", payload.get("no", [])))
        sequence = payload.get("sequence", message.get("sequence") if isinstance(message, dict) else None)
        if yes is None or no is None or isinstance(sequence, bool) or not isinstance(sequence, int):
            self.valid = False
            return False
        self.yes_bids, self.no_bids, self.sequence = yes, no, sequence
        self.received_at, self.valid = received_at or time.monotonic(), True
        return True

    def apply_delta(self, message: dict[str, Any], *, received_at: Optional[float] = None) -> bool:
        payload = message.get("msg", message) if isinstance(message, dict) else None
        if not self.valid or not isinstance(payload, dict):
            self.valid = False
            return False
        sequence = payload.get("sequence")
        side = str(payload.get("side", "")).casefold()
        try:
            price, delta = float(payload["price"]), float(payload["delta"])
        except (KeyError, TypeError, ValueError):
            self.valid = False
            return False
        if price > 1:
            price /= 100.0
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence != self.sequence + 1:
            self.valid = False
            return False
        destination = self.yes_bids if side == "yes" else self.no_bids if side == "no" else None
        if destination is None or not (0 < price < 1):
            self.valid = False
            return False
        quantity = destination.get(price, 0.0) + delta
        if quantity < 0:
            self.valid = False
            return False
        if quantity:
            destination[price] = quantity
        else:
            destination.pop(price, None)
        self.sequence, self.received_at = sequence, received_at or time.monotonic()
        return True

    def fresh(self, max_age_seconds: float, *, now: Optional[float] = None) -> bool:
        return bool(self.valid and self.received_at is not None and (now or time.monotonic()) - self.received_at <= max_age_seconds)

    def executable_quantity(self, side: str, maximum_price: float) -> float:
        """Contracts purchasable at a buy limit, converting opposite-side bids."""
        source = self.no_bids if side.upper() == "YES" else self.yes_bids if side.upper() == "NO" else None
        if source is None or not self.valid or not 0 < maximum_price < 1:
            return 0.0
        return sum(quantity for bid, quantity in source.items() if 1 - bid <= maximum_price + 1e-9)


@dataclass(frozen=True)
class ReferencePrice:
    price: float
    observed_at: float
    source: str
    kalshi_reference_price: Optional[float] = None


@dataclass(frozen=True)
class KXBTC15MSettings:
    enabled: bool = False
    live_execution_enabled: bool = False
    cutoff_seconds: int = 60
    max_ws_age_seconds: float = 5.0
    max_reference_age_seconds: float = 5.0
    max_reference_displacement_bps: float = 25.0
    min_confidence: float = 0.75
    min_net_edge: float = 0.05
    max_spread: float = 0.08
    min_liquidity: float = 1.0


@dataclass(frozen=True)
class KXBTC15MDecision:
    action: str
    side: Optional[str]
    reason: str
    p_up: Optional[float] = None
    p_down: Optional[float] = None
    implied_probability: Optional[float] = None
    gross_edge: Optional[float] = None
    net_edge: Optional[float] = None
    confidence: Optional[float] = None
    seconds_remaining: Optional[float] = None
    target_cushion: Optional[float] = None
    liquidity: float = 0.0


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def _returns(prices: list[float]) -> list[float]:
    return [math.log(current / previous) for previous, current in zip(prices, prices[1:]) if previous > 0 and current > 0]


class KXBTC15MSignalEngine:
    """Settlement-aware probability estimate using a verified compatible feed."""

    def __init__(self, settings: KXBTC15MSettings) -> None:
        self.settings = settings

    def evaluate(
        self, *, market: dict[str, Any], target_price: float, reference: ReferencePrice,
        price_history: list[float], book: ReconstructedOrderBook,
        yes_bid: float, yes_ask: float, no_bid: float, no_ask: float,
        fee_rate: float, slippage_rate: float, now: Optional[float] = None,
        wall_clock: Optional[datetime] = None,
    ) -> KXBTC15MDecision:
        current = now or time.monotonic()
        remaining = seconds_to_expiration(market, wall_clock)
        if remaining is None:
            return KXBTC15MDecision("NO_TRADE", None, "authoritative close time unavailable")
        if remaining <= self.settings.cutoff_seconds:
            return KXBTC15MDecision("NO_TRADE", None, "near-expiration entry cutoff", seconds_remaining=remaining)
        if not book.fresh(self.settings.max_ws_age_seconds, now=current):
            return KXBTC15MDecision("NO_TRADE", None, "WebSocket order book is stale or sequence-invalid", seconds_remaining=remaining)
        if current - reference.observed_at > self.settings.max_reference_age_seconds:
            return KXBTC15MDecision("NO_TRADE", None, "reference price is stale", seconds_remaining=remaining)
        if reference.kalshi_reference_price is None or reference.kalshi_reference_price <= 0:
            return KXBTC15MDecision("NO_TRADE", None, "Kalshi-compatible reference price unavailable", seconds_remaining=remaining)
        displacement = abs(reference.price - reference.kalshi_reference_price) / reference.kalshi_reference_price * 10_000
        if displacement > self.settings.max_reference_displacement_bps:
            return KXBTC15MDecision("NO_TRADE", None, "external reference is incompatible with Kalshi reference", seconds_remaining=remaining)
        if not all(0 < value < 1 for value in (yes_ask, no_ask)) or not (0 <= yes_bid <= yes_ask and 0 <= no_bid <= no_ask):
            return KXBTC15MDecision("NO_TRADE", None, "valid executable bid/ask unavailable", seconds_remaining=remaining)
        returns = _returns(price_history + [reference.price])
        if len(returns) < 5:
            return KXBTC15MDecision("NO_TRADE", None, "insufficient compatible BTC price history", seconds_remaining=remaining)
        volatility = max(1e-6, pstdev(returns))
        drift = mean(returns[-5:])
        horizon = max(remaining / 60.0, 1.0)
        z = (math.log(target_price / reference.price) - drift * horizon) / (volatility * math.sqrt(horizon))
        p_up = min(0.999, max(0.001, 1 - _normal_cdf(z)))
        p_down = 1 - p_up
        side, probability, bid, ask = max(
            (("YES", p_up, yes_bid, yes_ask), ("NO", p_down, no_bid, no_ask)), key=lambda item: item[1] - item[3]
        )
        spread = ask - bid
        gross = probability - ask
        net = gross - spread - fee_rate - slippage_rate
        liquidity = book.executable_quantity(side, ask)
        cushion = (reference.price - target_price) if side == "YES" else (target_price - reference.price)
        confidence = min(1.0, max(0.0, 0.45 + min(0.35, abs(cushion) / max(target_price * volatility * math.sqrt(horizon), 1.0)) + min(0.2, len(returns) / 100)))
        fields = dict(p_up=p_up, p_down=p_down, implied_probability=ask, gross_edge=gross, net_edge=net, confidence=confidence, seconds_remaining=remaining, target_cushion=cushion, liquidity=liquidity)
        if confidence < self.settings.min_confidence:
            return KXBTC15MDecision("NO_TRADE", side, "model confidence below BTC15M minimum", **fields)
        if gross > 0.25:
            return KXBTC15MDecision("NO_TRADE", side, "extreme model-market discrepancy requires independent validation", **fields)
        if net < self.settings.min_net_edge:
            return KXBTC15MDecision("NO_TRADE", side, "net edge below BTC15M minimum after fees and slippage", **fields)
        if spread > self.settings.max_spread:
            return KXBTC15MDecision("NO_TRADE", side, "bid/ask spread exceeds BTC15M limit", **fields)
        if liquidity < self.settings.min_liquidity:
            return KXBTC15MDecision("NO_TRADE", side, "insufficient executable order-book liquidity", **fields)
        return KXBTC15MDecision("TRADE_" + side, side, "strong compatible-reference BTC15M edge", **fields)


def position_action(*, existing_side: Optional[str], existing_quantity: float, decision: KXBTC15MDecision, entry_probability: Optional[float]) -> str:
    """Conservative add/hold/reduce classification; never averages down."""
    if existing_quantity <= 0 or existing_side is None:
        return decision.action
    if decision.side != existing_side:
        return "REDUCE_EXIT" if decision.action.startswith("TRADE_") else "HOLD"
    if decision.action.startswith("TRADE_") and entry_probability is not None and (decision.p_up if existing_side == "YES" else decision.p_down) > entry_probability + 0.05:
        return "ADD"
    return "DO_NOT_ADD"

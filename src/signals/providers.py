from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable

from src.strategies.directional_policy import executable_liquidity
from src.utils.market_prices import get_market_prices

from .models import MarketSignalContext, SignalEstimate


class SignalProvider(ABC):
    name: str
    categories: frozenset[str] = frozenset()

    def supports(self, context: MarketSignalContext) -> bool:
        return not self.categories or context.category.casefold() in self.categories

    @abstractmethod
    def estimate(self, context: MarketSignalContext) -> SignalEstimate | None: ...


class OrderBookSignalProvider(SignalProvider):
    name = "kalshi_orderbook"

    def estimate(self, context: MarketSignalContext) -> SignalEstimate | None:
        yes_bid, yes_ask, no_bid, no_ask = get_market_prices(context.market)
        if not (0 < yes_ask < 1 and 0 < no_ask < 1):
            return None
        yes_depth = executable_liquidity(context.orderbook, "YES", yes_ask)
        no_depth = executable_liquidity(context.orderbook, "NO", no_ask)
        total = yes_depth + no_depth
        if total <= 0:
            return None
        midpoint = (yes_bid + yes_ask) / 2
        imbalance = (yes_depth - no_depth) / total
        probability = min(.99, max(.01, midpoint + .04 * imbalance))
        spread = max(0.0, yes_ask - yes_bid)
        reliability = min(.75, max(.15, .65 - spread + min(total, 1000) / 10000))
        return SignalEstimate(
            self.name, probability, reliability, context.now,
            "kalshi_microstructure", int(total),
            {"yes_depth": yes_depth, "no_depth": no_depth, "spread": spread,
             "imbalance": imbalance},
        )


class RelatedMarketSignalProvider(SignalProvider):
    name = "related_kalshi_markets"

    def estimate(self, context: MarketSignalContext) -> SignalEstimate | None:
        values = context.related_probabilities
        if not values or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in values):
            return None
        probability = sum(values) / len(values)
        return SignalEstimate(
            self.name, probability, min(.7, .3 + .05 * len(values)), context.now,
            "kalshi_related", len(values), {"related_count": len(values)},
        )


class InjectedPublicSignalProvider(SignalProvider):
    """Route prevalidated lawful public observations supplied by category adapters."""

    name = "public_evidence"

    def estimate(self, context: MarketSignalContext) -> SignalEstimate | None:
        return None

    def estimate_many(self, context: MarketSignalContext) -> Iterable[SignalEstimate]:
        return context.external_signals


def default_providers() -> tuple[SignalProvider, ...]:
    return (OrderBookSignalProvider(), MarketHistorySignalProvider(), RelatedMarketSignalProvider())


class MarketHistorySignalProvider(SignalProvider):
    """Conservative momentum/volume-change signal from caller-supplied snapshots."""

    name = "kalshi_market_history"

    def estimate(self, context: MarketSignalContext) -> SignalEstimate | None:
        probabilities = context.market.get("_recent_yes_probabilities")
        volumes = context.market.get("_recent_volumes")
        if not isinstance(probabilities, (list, tuple)) or len(probabilities) < 3:
            return None
        try:
            values = tuple(float(value) for value in probabilities)
        except (TypeError, ValueError):
            return None
        if any(not 0 < value < 1 for value in values):
            return None
        momentum = max(-.05, min(.05, values[-1] - values[0]))
        volume_reliability = .25
        if isinstance(volumes, (list, tuple)) and len(volumes) >= 2:
            try:
                start, end = float(volumes[0]), float(volumes[-1])
                if start >= 0 and end > start:
                    volume_reliability = min(.5, .25 + (end - start) / max(1000, end) * .25)
            except (TypeError, ValueError):
                return None
        return SignalEstimate(
            self.name, max(.01, min(.99, values[-1] + momentum * .25)),
            volume_reliability, context.now, "kalshi_microstructure", len(values),
            {"momentum": momentum, "volume_change_available": volumes is not None},
        )

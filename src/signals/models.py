from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class SignalEstimate:
    provider: str
    probability_yes: float
    reliability: float
    observed_at: datetime
    correlation_group: str
    sample_size: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = (self.probability_yes, self.reliability)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise TypeError("signal probability and reliability must be numeric")
        if not 0 <= self.probability_yes <= 1 or not 0 <= self.reliability <= 1:
            raise ValueError("signal probability and reliability must be in [0, 1]")
        if self.observed_at.tzinfo is None:
            raise ValueError("signal timestamp must be timezone-aware")


@dataclass(frozen=True)
class MarketSignalContext:
    market_id: str
    category: str
    phase: str
    market: dict[str, Any]
    orderbook: dict[str, Any]
    requested_quantity: float
    external_signals: tuple[SignalEstimate, ...] = ()
    related_probabilities: tuple[float, ...] = ()
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ConsensusForecast:
    market_id: str
    probability_yes: float
    calibrated_confidence: float
    disagreement: float
    agreement: float
    signals: tuple[SignalEstimate, ...]
    rejected_signals: tuple[str, ...]
    data_freshness_seconds: float


@dataclass(frozen=True)
class MispricingEvaluation:
    market_id: str
    side: str
    executable_price: float
    market_implied_probability: float
    fair_probability: float
    gross_edge: float
    fee_dollars: float | None
    spread_dollars: float
    slippage_dollars: float
    net_ev_pct: float
    net_ev_dollars: float
    executable_liquidity: float
    proposed_quantity: float
    accepted: bool
    reason: str

"""Explicit simulated execution assumptions."""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.0
    slippage: float = 0.0
    spread_fraction: float = 1.0
    latency_penalty: float = 0.0
    fill_fraction: float = 1.0

    def __post_init__(self) -> None:
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in (self.fee_rate, self.slippage, self.spread_fraction, self.latency_penalty)):
            raise ValueError("cost assumptions must be finite and between zero and one")
        if not math.isfinite(self.fill_fraction) or not 0 <= self.fill_fraction <= 1:
            raise ValueError("fill_fraction must be between zero and one")

    def entry_price(self, quoted_price: float, spread: float) -> float:
        return min(1.0, quoted_price + spread * self.spread_fraction + self.slippage + self.latency_penalty)

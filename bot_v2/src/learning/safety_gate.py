from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class LearningSafetyDecision:
    allowed: bool
    multiplier: float
    reasons: list[str]


class LearningSafetyGate:
    def __init__(
        self,
        minimum_closed_trades: int = 30,
        maximum_multiplier: float = 1.10,
        minimum_multiplier: float = 0.50,
    ) -> None:
        self.minimum_closed_trades = minimum_closed_trades
        self.maximum_multiplier = maximum_multiplier
        self.minimum_multiplier = minimum_multiplier

    def evaluate(
        self,
        closed_trades: int,
        strategy_multiplier: float,
        confidence_multiplier: float,
        paper_mode: bool = True,
    ) -> dict[str, Any]:
        reasons: list[str] = []

        if closed_trades < self.minimum_closed_trades:
            reasons.append(
                f"Only {closed_trades} closed trades; "
                f"{self.minimum_closed_trades} required."
            )

        if not paper_mode:
            reasons.append(
                "Automatic learning adjustments are disabled for live trading."
            )

        allowed = (
            closed_trades >= self.minimum_closed_trades
            and paper_mode
        )

        proposed = strategy_multiplier * confidence_multiplier
        multiplier = max(
            self.minimum_multiplier,
            min(self.maximum_multiplier, proposed),
        )

        if not allowed:
            multiplier = 1.0

        return asdict(
            LearningSafetyDecision(
                allowed=allowed,
                multiplier=round(multiplier, 4),
                reasons=reasons,
            )
        )

"""Position sizing and simulated-account risk limits.

Sizing depends ONLY on current equity, the entry price and observable
liquidity. It never depends on recent wins/losses, so martingale, doubling
down and recovery betting are impossible by construction (and tested).

Worst-case loss per contract is the full premium plus the entry fee: a binary
contract can settle at $0, and a stop is not a guaranteed exit price.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math


@dataclass(frozen=True)
class RiskConfig:
    starting_balance: float = 1000.0
    risk_per_trade: float = 0.01  # fraction of equity at worst-case loss
    max_total_exposure: float = 0.05  # open cost basis / equity
    max_contracts_per_order: int = 1000
    liquidity_fraction: float = 0.25  # of displayed depth within limit price
    daily_loss_limit: float = 0.03  # of start-of-UTC-day equity
    max_consecutive_losses: int = 5
    consecutive_loss_pause_seconds: float = 3600.0
    max_drawdown_kill: float = 0.10  # from peak equity: stop simulation

    def validate(self):
        assert self.starting_balance > 0
        assert 0 < self.risk_per_trade <= 0.05, "risk per trade above 5% is refused"
        assert 0 < self.max_total_exposure <= 0.5
        assert 0 < self.liquidity_fraction <= 1
        assert 0 < self.daily_loss_limit <= 0.5
        assert self.max_consecutive_losses >= 1
        assert 0 < self.max_drawdown_kill <= 0.5
        return self

    def to_dict(self):
        return asdict(self)


def taker_fee(price: float, contracts: float, multiplier: float = 1.0) -> float:
    """Kalshi taker fee: ceil to the cent of 0.07 * C * P * (1-P), per execution."""
    if contracts <= 0:
        return 0.0
    return math.ceil(0.07 * multiplier * contracts * price * (1 - price) * 100 - 1e-9) / 100


def utc_day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.c = config.validate()
        self.peak = config.starting_balance
        self.day = None
        self.day_start_equity = config.starting_balance
        self.day_halted = False
        self.consecutive_losses = 0
        self.paused_until = -math.inf
        self.killed = False
        self.kill_reason = None
        self.events = []  # (t, event, detail)

    def size(self, equity: float, price: float, displayed_qty: float | None, exposure: float, multiplier=1.0):
        """Whole contracts to request, or (0, reason)."""
        if equity <= 0 or not 0 < price < 1:
            return 0, "invalid equity or price"
        per_contract_worst = price + taker_fee(price, 1, multiplier)
        by_risk = math.floor(self.c.risk_per_trade * equity / per_contract_worst)
        room = self.c.max_total_exposure * equity - exposure
        by_exposure = math.floor(max(0.0, room) / per_contract_worst)
        qty = min(by_risk, by_exposure, self.c.max_contracts_per_order)
        reason = "1% worst-case risk"
        if displayed_qty is not None:
            by_liquidity = math.floor(self.c.liquidity_fraction * displayed_qty)
            if by_liquidity < qty:
                qty, reason = by_liquidity, "limited by displayed liquidity"
        if by_exposure < by_risk and by_exposure == qty:
            reason = "limited by total exposure cap"
        if qty < 1:
            return 0, "size rounds to zero contracts (" + reason + ")"
        return qty, reason

    def can_enter(self, t: float):
        if self.killed:
            return False, "KILL SWITCH: " + (self.kill_reason or "risk limit exceeded")
        if self.day_halted:
            return False, "daily loss limit reached; no new trades until next UTC day"
        if t < self.paused_until:
            return False, f"paused after {self.c.max_consecutive_losses} consecutive losses"
        return True, ""

    def must_flatten(self):
        return self.killed or self.day_halted

    def update(self, t: float, equity: float):
        day = utc_day(t)
        if day != self.day:
            self.day, self.day_start_equity, self.day_halted = day, equity, False
        self.peak = max(self.peak, equity)
        if not self.day_halted and equity < self.day_start_equity * (1 - self.c.daily_loss_limit):
            self.day_halted = True
            self.events.append((t, "DAILY_LOSS_HALT", round(equity, 2)))
        if not self.killed and equity < self.peak * (1 - self.c.max_drawdown_kill):
            self.killed = True
            self.kill_reason = f"drawdown exceeded {self.c.max_drawdown_kill:.0%} of peak equity"
            self.events.append((t, "KILL_SWITCH", round(equity, 2)))

    def on_trade_closed(self, t: float, net_pnl: float):
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.c.max_consecutive_losses:
                self.paused_until = t + self.c.consecutive_loss_pause_seconds
                self.events.append((t, "CONSECUTIVE_LOSS_PAUSE", self.consecutive_losses))
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0

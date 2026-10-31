"""Pure offline estimates from explicitly supplied conditional scenario paths.

Scenario frequencies are model estimates, never calibrated live probabilities.
Prices are dollars per contract; profit and loss limits are net dollars for
one contract. Each path contains executable UP/DOWN bids at increasing seconds
from the snapshot, plus the official-reference settlement direction.
"""
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any


@dataclass(frozen=True)
class Display:
    status: str
    reason: str
    contract: str | None = None
    settlement_up: float | None = None
    settlement_down: float | None = None
    scalp_up: float | None = None
    scalp_down: float | None = None
    samples: int = 0
    label: str = "UNVALIDATED — conditional scenario frequencies; not calibrated probabilities"


def number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a numeric value")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("numeric value outside supported bounds")
    return float(value)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include timezone")
    return parsed


def fee(price: float, multiplier: float) -> float:
    # One-contract taker fee, rounded up to cents on each leg.
    return math.ceil(0.07 * multiplier * price * (1 - price) * 100 - 1e-12) / 100


def evaluate(
    payload: dict, *, now: datetime, target: float = 0.05, stop: float = 0.03, horizon: float = 60
) -> Display:
    """Fail closed on stale/ambiguous data. No I/O or order operations.

    The caller supplies equally weighted, independent conditional model paths.
    Both sides use the same paths, but scalp success events are independent
    questions and need not sum to one. Timeouts count as non-successes.
    """
    try:
        if now.tzinfo is None:
            raise ValueError("now must include timezone")
        target = number(target, 0.000001, 1)
        stop = number(stop, 0.000001, 1)
        horizon = number(horizon, 0.001, 900)
        candidates = []
        for market in payload["markets"]:
            if market["series"] != "KXBTC15M" or market["status"] != "open":
                continue
            opened, closes = timestamp(market["opens_at"]), timestamp(market["closes_at"])
            if not 0 < (closes - opened).total_seconds() <= 900:
                raise ValueError("contract is not a verified 15-minute interval")
            if opened <= now < closes:
                candidates.append(market)
        if len(candidates) != 1:
            raise ValueError("active contract missing or ambiguous")
        market = candidates[0]
        ticker = market["ticker"]
        if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
            raise ValueError("invalid contract identity")
        if (
            not market["settlement_reference"]
            or market["up_definition"] != "reference_at_settlement > strike"
        ):
            raise ValueError("explicit settlement reference and UP definition required")
        number(market["strike"], 0.000001, 1e12)
        age = (now - timestamp(market["observed_at"])).total_seconds()
        if not 0 <= age <= 5:
            raise ValueError("snapshot stale or future-dated (maximum age 5s)")
        remaining = (timestamp(market["closes_at"]) - now).total_seconds()
        quotes = market["quotes"]
        for side in ("up", "down"):
            bid = number(quotes[side]["bid"], 0.000001, 0.999999)
            ask = number(quotes[side]["ask"], bid, 0.999999)
            number(quotes[side]["bid_size"], 1, 1e12)
            number(quotes[side]["ask_size"], 1, 1e12)
        if (
            quotes["up"]["bid"] + quotes["down"]["bid"] > 1
            or quotes["up"]["ask"] + quotes["down"]["ask"] < 1
        ):
            raise ValueError("inconsistent binary quotes")
        evidence = payload["evidence"]
        if (
            evidence["ticker"] != ticker
            or evidence["snapshot_at"] != market["observed_at"]
            or evidence["settlement_reference"] != market["settlement_reference"]
            or evidence["strike"] != market["strike"]
        ):
            raise ValueError("scenario evidence does not match contract snapshot")
        if not isinstance(evidence["method"], str) or not evidence["method"].strip():
            raise ValueError("scenario provenance required")
        paths = evidence["paths"]
        if len(paths) < 30:
            raise ValueError("insufficient conditional scenarios (minimum 30)")
        outcomes = []
        for path in paths:
            if type(path["settlement_up"]) is not bool:
                raise ValueError("each scenario needs a settlement outcome")
            outcomes.append(path["settlement_up"])
        up = sum(outcomes) / len(paths)
        base = dict(contract=ticker, settlement_up=up, settlement_down=1 - up, samples=len(paths))
        try:
            if horizon > remaining:
                raise ValueError("scalp time limit exceeds remaining contract lifetime")
            if market["fee_type"] not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("unsupported fee schedule")
            multiplier = number(market["fee_multiplier"], 0, 100)
            successes = {"up": 0, "down": 0}
            for path in paths:
                points = path["quotes"]
                previous = -1.0
                resolved = {"up": None, "down": None}
                for point in points:
                    seconds = number(point["seconds"], 0, 900)
                    if seconds <= previous:
                        raise ValueError("scenario times must be strictly increasing")
                    if previous < horizon < seconds:
                        raise ValueError("scenario must include exact time-limit observation")
                    previous = seconds
                    if number(point["up_bid"], 0, 1) + number(point["down_bid"], 0, 1) > 1:
                        raise ValueError("inconsistent scenario binary bids")
                    for side in ("up", "down"):
                        bid = number(point[side + "_bid"], 0, 1)
                        if seconds == 0 and bid != quotes[side]["bid"]:
                            raise ValueError("scenario initial bids must match snapshot")
                        entry = quotes[side]["ask"]
                        pnl = bid - entry - fee(entry, multiplier) - fee(bid, multiplier)
                        if resolved[side] is None and seconds <= horizon:
                            if pnl <= -stop:
                                resolved[side] = False
                            elif seconds < horizon and pnl >= target:
                                resolved[side] = True
                if not points or points[0]["seconds"] != 0 or previous < horizon:
                    raise ValueError("scenario must cover entry through time limit")
                for side in successes:
                    successes[side] += resolved[side] is True
            return Display(
                "ESTIMATE",
                "Offline model evidence only; no execution recommendation",
                **base,
                scalp_up=successes["up"] / len(paths),
                scalp_down=successes["down"] / len(paths),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            return Display("WAIT", f"Scalp unavailable: {exc}", **base)
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError) as exc:
        return Display("WAIT", f"Estimates unavailable: {exc}")

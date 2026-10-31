import math


def calculate_taker_fee(*, fee_type: str, fee_multiplier: float,
                        price: float, quantity: float) -> float | None:
    """Calculate a quoted taker fee from authoritative series metadata.

    Kalshi's quadratic base coefficient is 0.07.  The series multiplier is
    applied to that coefficient. Unknown fee models fail closed.
    """
    values = (fee_multiplier, price, quantity)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    if fee_multiplier < 0 or not 0 < price < 1 or quantity <= 0:
        return None
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        return None
    raw = 0.07 * fee_multiplier * quantity * price * (1 - price)
    return math.ceil(raw * 100 - 1e-12) / 100

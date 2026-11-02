"""Past-only adaptive momentum descriptors for shadow evaluation.

No fixed dollar move, Kalshi price, or handcrafted score becomes a probability.
The learner may evaluate these raw descriptors only through its chronological
feature-selection and untouched-holdout protocol.
"""
import math


FEATURES = (
    "adaptive_momentum_consistency",
    "adaptive_momentum_strength",
    "adaptive_threshold_distance",
    "adaptive_acceleration",
    "adaptive_reversal",
)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def describe(values, market=None):
    result = {name: None for name in FEATURES}
    returns = [values.get(name) for name in ("return_1m", "return_5m", "return_15m")]
    valid = [value for value in returns if _finite(value)]
    volatility = values.get("volatility_24h")
    remaining = values.get("remaining_fraction")
    distance = values.get("distance_scaled")
    acceleration = values.get("acceleration")
    if valid:
        signs = [1 if value > 0 else -1 if value < 0 else 0 for value in valid]
        result["adaptive_momentum_consistency"] = sum(signs) / len(signs)
    if len(valid) == 3 and _finite(volatility) and volatility > 0:
        # Convert the per-sqrt-second fractional volatility to each return horizon.
        standardized = [
            value / (volatility * math.sqrt(seconds))
            for value, seconds in zip(returns, (60, 300, 900))
        ]
        result["adaptive_momentum_strength"] = sum(standardized) / 3
    if all(_finite(value) for value in (distance, volatility, remaining)) and volatility > 0:
        seconds = max(1.0, 900 * remaining)
        result["adaptive_threshold_distance"] = distance / (volatility * math.sqrt(seconds))
    if _finite(acceleration) and _finite(volatility) and volatility > 0:
        result["adaptive_acceleration"] = acceleration / (volatility * math.sqrt(60))
    if len(valid) >= 2:
        result["adaptive_reversal"] = int(valid[0] * valid[1] < 0)

    disagreement = None
    try:
        up = market["quotes"]["up"]
        if _finite(up.get("bid")) and _finite(up.get("ask")) and up["bid"] <= up["ask"]:
            disagreement = {
                "up_executable_bid": up["bid"],
                "up_executable_ask": up["ask"],
                "note": "Kalshi executable prices are a separate benchmark and are never adaptive momentum inputs.",
            }
    except (KeyError, TypeError):
        pass
    return {
        "status": "EXPERIMENTAL_SHADOW" if any(v is not None for v in result.values()) else "WARMING_UP",
        "features": result,
        "market_benchmark": disagreement,
        "method": "Volatility-normalized threshold distance, multi-horizon return consistency, acceleration and reversal; thresholds must be learned chronologically rather than assumed.",
        "actionable": False,
    }

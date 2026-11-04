"""Uncalibrated time-to-settlement probability diagnostics (shadow only)."""
from __future__ import annotations

from decimal import Decimal
import math

from .contract_rules import decimal_value


def _cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def estimate(*, reference, strike, seconds_remaining, realized_volatility_per_sqrt_second,
             jump_volatility_per_sqrt_second=0, data_uncertainty=0,
             comparison_operator=">=", averaging_remaining_fraction=1):
    """Return a transparent diffusion diagnostic, never a promoted probability."""
    try:
        price = decimal_value(reference, low=Decimal("0.00000001"))
        threshold = decimal_value(strike, low=Decimal("0.00000001"))
        seconds = decimal_value(seconds_remaining, low=Decimal("0"), high=Decimal("86400"))
        realized = decimal_value(realized_volatility_per_sqrt_second, low=Decimal("0"))
        jump = decimal_value(jump_volatility_per_sqrt_second, low=Decimal("0"))
        uncertainty = decimal_value(data_uncertainty, low=Decimal("0"))
        fraction = decimal_value(averaging_remaining_fraction, low=Decimal("0"), high=Decimal("1"))
        if comparison_operator not in {">", ">=", "<", "<="}:
            raise ValueError("unsupported comparison operator")
        sigma = ((realized * realized + jump * jump) * seconds * fraction + uncertainty * uncertainty).sqrt()
        if sigma == 0:
            if price == threshold:
                yes = Decimal(1) if comparison_operator in {">=", "<="} else Decimal(0)
            else:
                probability_above = Decimal(1 if price > threshold else 0)
                yes = probability_above if comparison_operator in {">", ">="} else Decimal(1) - probability_above
        else:
            probability_above = Decimal(str(_cdf(float((price - threshold) / sigma))))
            yes = probability_above if comparison_operator in {">", ">="} else Decimal(1) - probability_above
        return {
            "status": "SHADOW_UNCALIBRATED", "probability_yes": str(yes),
            "probability_no": str(Decimal(1) - yes), "distance_from_strike": str(price - threshold),
            "effective_sigma": str(sigma), "seconds_remaining": str(seconds),
            "jump_risk_sigma": str(jump), "data_uncertainty": str(uncertainty),
            "averaging_remaining_fraction": str(fraction),
            "assumptions": "Zero-drift diffusion sensitivity with explicit jump and data uncertainty; not a calibrated live model.",
            "actionable": False,
        }
    except (ValueError, TypeError, ArithmeticError) as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "actionable": False}


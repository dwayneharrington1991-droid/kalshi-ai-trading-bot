from src.strategies.directional_policy import executable_liquidity
from src.utils.market_prices import get_market_prices

from .models import ConsensusForecast, MarketSignalContext, MispricingEvaluation


def evaluate_mispricing(context: MarketSignalContext, forecast: ConsensusForecast, *,
                        fee_dollars: float | None, slippage_dollars: float,
                        minimum_probability: float = .55, minimum_edge: float = .05,
                        minimum_confidence: float = .35) -> MispricingEvaluation:
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(context.market)
    choices = (("YES", forecast.probability_yes, yes_bid, yes_ask),
               ("NO", 1 - forecast.probability_yes, no_bid, no_ask))
    side, fair, bid, price = max(choices, key=lambda item: item[1] - item[3])
    spread = max(0.0, price - bid) * context.requested_quantity
    gross_edge = fair - price
    liquidity = executable_liquidity(context.orderbook, side, price)
    gross_ev = gross_edge * context.requested_quantity
    net_dollars = (
        gross_ev - fee_dollars - spread - slippage_dollars
        if fee_dollars is not None else float("-inf")
    )
    net_pct = net_dollars / max(.01, price * context.requested_quantity)
    reason = "multi-signal evidence supports positive executable net EV"
    accepted = True
    if fee_dollars is None:
        accepted, reason = False, "applicable Kalshi fee could not be verified"
    elif fair < minimum_probability:
        accepted, reason = False, "fair probability below preferred minimum"
    elif forecast.calibrated_confidence < minimum_confidence:
        accepted, reason = False, "calibrated evidence confidence below minimum"
    elif gross_edge < minimum_edge:
        accepted, reason = False, "gross edge below minimum"
    elif gross_edge > .25:
        accepted, reason = False, "extreme discrepancy requires independent verification"
    elif liquidity < context.requested_quantity:
        accepted, reason = False, "insufficient executable order-book depth"
    elif net_dollars <= 0:
        accepted, reason = False, "net dollar EV is not positive"
    return MispricingEvaluation(
        context.market_id, side, price, price, fair, gross_edge, fee_dollars,
        spread, slippage_dollars, net_pct, net_dollars, liquidity,
        context.requested_quantity, accepted, reason,
    )

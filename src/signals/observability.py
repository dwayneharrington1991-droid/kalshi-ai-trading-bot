import json
import logging
import math

from .models import ConsensusForecast, MispricingEvaluation


def log_signal_decision(logger: logging.Logger, *, category: str, phase: str,
                        forecast: ConsensusForecast,
                        decision: MispricingEvaluation) -> None:
    payload = {
        "market": decision.market_id, "category": category, "phase": phase,
        "side": decision.side, "executable_price": decision.executable_price,
        "market_implied_probability": decision.market_implied_probability,
        "model_fair_probability": decision.fair_probability,
        "calibrated_confidence": forecast.calibrated_confidence,
        "gross_edge": decision.gross_edge, "fees": decision.fee_dollars,
        "spread": decision.spread_dollars,
        "estimated_slippage": decision.slippage_dollars,
        "net_ev_pct": decision.net_ev_pct if math.isfinite(decision.net_ev_pct) else None,
        "net_ev_dollars": decision.net_ev_dollars if math.isfinite(decision.net_ev_dollars) else None,
        "executable_liquidity": decision.executable_liquidity,
        "proposed_quantity": decision.proposed_quantity,
        "signals": [{"provider": signal.provider,
                     "probability_yes": signal.probability_yes,
                     "reliability": signal.reliability,
                     "correlation_group": signal.correlation_group}
                    for signal in forecast.signals],
        "signal_agreement": forecast.agreement,
        "data_freshness_seconds": forecast.data_freshness_seconds,
        "rejected_signals": list(forecast.rejected_signals),
        "final_decision": "ACCEPT" if decision.accepted else "REJECT",
        "exact_reason": decision.reason,
    }
    logger.info("MULTI_SIGNAL_DECISION %s", json.dumps(payload, sort_keys=True, allow_nan=False))

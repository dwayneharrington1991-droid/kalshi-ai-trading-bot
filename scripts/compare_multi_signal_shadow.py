#!/usr/bin/env python3
"""Bounded GET-only comparison of current and multi-signal admission policies."""

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
import json
import math
import queue
import threading
from typing import Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv()

from scripts.preview_directional_opportunities import _as_market, _volume
from scripts.read_only_validate import ReadOnlyAccountClient, validate_read_only_safety
from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.config.settings import settings
from src.markets.discovery import MarketDiscovery72h
from src.signals.engine import MultiSignalEngine
from src.signals.fees import calculate_taker_fee
from src.signals.mispricing import evaluate_mispricing
from src.signals.models import MarketSignalContext, SignalEstimate
from src.signals.orderbook import executable_quote
from src.strategies.directional_policy import (
    MAX_UNVALIDATED_PROBABILITY_GAP,
    classify_market_phase,
    evaluate_directional_candidate,
    validate_directional_model_confidence,
)
from src.strategies.portfolio.immediate import _get_fast_ai_prediction
from src.utils.market_prices import get_market_prices, is_tradeable_market


def _series_metadata(response):
    value = response.get("series") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else None


async def _prediction_with_fresh_client(
    market, market_price: float,
) -> tuple[Optional[float], Optional[float]]:
    """Run one model request without sharing the comparison event loop's client."""
    model = XAIClient()
    try:
        return await _get_fast_ai_prediction(market, model, market_price)
    finally:
        try:
            await model.close()
        except Exception:
            # This is a diagnostic-only resource cleanup path. Never let it
            # suppress the comparison report or expose provider details.
            pass


def _prediction_worker(
    result_queue: queue.Queue,
    market,
    market_price: float,
) -> None:
    """Daemon worker boundary for an uncooperative model-provider call."""
    try:
        probability, confidence = asyncio.run(
            _prediction_with_fresh_client(market, market_price)
        )
        result_queue.put(("ok", probability, confidence))
    except BaseException:
        # Provider exceptions can include request metadata. Report only a
        # stable category to the parent process.
        result_queue.put(("failure", None, None))


async def isolated_model_prediction(
    market,
    market_price: float,
    timeout_seconds: float = 20.0,
) -> tuple[Optional[float], Optional[float], str]:
    """Return a bounded prediction even if a provider ignores cancellation.

    A daemon worker cannot hold the comparison's event loop hostage. On a
    timeout the caller opens its provider circuit breaker, so no additional
    model calls are started and the final report is always produced.
    """
    result_queue: queue.Queue = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_prediction_worker,
        args=(result_queue, market, market_price),
        daemon=True,
        name="multi-signal-shadow-model",
    )
    worker.start()
    try:
        state, probability, confidence = await asyncio.to_thread(
            result_queue.get, True, timeout_seconds
        )
    except queue.Empty:
        return None, None, "timeout"
    if state != "ok":
        return None, None, "failure"
    return probability, confidence, "ok"


async def run(sample_size: int, model_timeout_seconds: float = 20.0) -> int:
    environment = validate_read_only_safety(os.environ)
    if os.getenv("LIVE_TRADING_ENABLED", "").lower() != "false":
        raise RuntimeError("LIVE_TRADING_ENABLED must be explicitly false")
    raw_client = KalshiClient(environment=environment)
    client = ReadOnlyAccountClient(raw_client, environment)
    try:
        discovery = await MarketDiscovery72h(client).discover()
        universe = [market for market in discovery.markets if is_tradeable_market(market)]
        universe.sort(key=_volume, reverse=True)
        current_reasons, multi_reasons, providers = Counter(), Counter(), Counter()
        current_funnel = Counter()
        multi_funnel = Counter()
        phase_counts = Counter()
        current_accepted = multi_accepted = extreme = failures = 0
        failure_reasons = Counter()
        differences = []
        records = []
        series_cache = {}
        evaluated = attempted = 0
        model_provider_unresponsive = False
        deadline = time.monotonic() + 600
        for raw in universe:
            if attempted >= sample_size or time.monotonic() >= deadline:
                break
            attempted += 1
            market = _as_market(raw)
            operation = "model"
            try:
                if model_provider_unresponsive:
                    failures += 1
                    failure_reasons["model_provider_circuit_open"] += 1
                    continue
                predicted, model_confidence, prediction_state = await isolated_model_prediction(
                    market,
                    get_market_prices(raw)[1],
                    timeout_seconds=model_timeout_seconds,
                )
                if prediction_state == "timeout":
                    # A timed-out daemon may still be winding down, so fail
                    # closed and never initiate another external model call.
                    model_provider_unresponsive = True
                    failures += 1
                    failure_reasons["model_timeout"] += 1
                    continue
                if prediction_state != "ok":
                    failures += 1
                    failure_reasons["model_failure"] += 1
                    continue
                if predicted is None:
                    failures += 1
                    failure_reasons["model_unavailable"] += 1
                    continue
                quote_started = time.monotonic()
                operation = "market_orderbook"
                fresh_response, orderbook = await asyncio.wait_for(
                    asyncio.gather(
                        client.get_market(market.market_id),
                        client.get_orderbook(market.market_id, depth=100),
                    ),
                    timeout=15,
                )
                fresh = fresh_response.get("market") if isinstance(fresh_response, dict) else None
                if not isinstance(fresh, dict):
                    failures += 1
                    failure_reasons["malformed_market"] += 1
                    continue
                yes_bid, yes_ask, no_bid, no_ask = get_market_prices(fresh)
                phase = classify_market_phase(
                    fresh, market.category, expiration_ts=market.expiration_ts,
                    fast_live_minutes=settings.trading.fast_live_max_remaining_minutes,
                )
                phase_counts[phase] += 1
                current = evaluate_directional_candidate(
                    market_id=market.market_id, predicted_yes_probability=predicted,
                    yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
                    min_probability=settings.trading.min_preferred_probability,
                    max_preferred_probability=settings.trading.max_preferred_probability,
                    min_edge=settings.trading.min_directional_edge,
                    fee_estimate=settings.trading.directional_fee_estimate,
                    slippage_estimate=settings.trading.directional_slippage_estimate,
                )
                quantity = max(1, int(5 / current.market_implied_probability)) if current.market_implied_probability else 1
                current_quote = executable_quote(
                    orderbook, current.side, quantity,
                    maximum_price=min(.99, current.market_implied_probability + settings.trading.directional_slippage_estimate),
                )
                quote_age = time.monotonic() - quote_started
                confidence_ok, confidence_reason = validate_directional_model_confidence(model_confidence)
                current_probability_ok = (
                    current.estimated_probability >= settings.trading.min_preferred_probability
                    and current.market_implied_probability >= settings.trading.min_preferred_probability
                )
                current_edge_ok = (
                    settings.trading.min_directional_edge <= current.gross_edge
                    <= MAX_UNVALIDATED_PROBABILITY_GAP
                )
                current_market_ok = current_quote.fillable_quantity >= quantity and quote_age <= 5
                current_final = current.accepted and confidence_ok and current_market_ok
                current_final_reason = current.reason
                if current.accepted and not confidence_ok:
                    current_final_reason = confidence_reason
                elif current.accepted and not current_market_ok:
                    current_final_reason = "fresh executable depth cannot fill proposed quantity"
                current_reasons[current_final_reason] += 1
                current_funnel["probability"] += int(current_probability_ok)
                current_funnel["edge"] += int(current_probability_ok and current_edge_ok)
                current_funnel["confidence"] += int(current_probability_ok and current_edge_ok and confidence_ok)
                current_funnel["liquidity_freshness"] += int(current_probability_ok and current_edge_ok and confidence_ok and current_market_ok)
                current_funnel["final"] += int(current_final)
                current_accepted += int(current_final)
                context = MarketSignalContext(
                    market.market_id, market.category, phase,
                    fresh, orderbook, quantity,
                    (SignalEstimate(
                        "current_model", float(predicted), float(model_confidence),
                        datetime.now(timezone.utc), "independent_model", None,
                        {"authorized_source": True},
                    ),), tuple(raw.get("_related_probabilities", ())),
                )
                forecast = MultiSignalEngine().forecast(context)
                for signal in forecast.signals:
                    providers[signal.provider] += 1
                forecast_choices = (
                    (forecast.probability_yes - yes_ask, "YES", yes_ask),
                    ((1 - forecast.probability_yes) - no_ask, "NO", no_ask),
                )
                _, forecast_side, forecast_price = max(forecast_choices)
                max_price = min(.99, forecast_price + settings.trading.directional_slippage_estimate)
                quote = executable_quote(orderbook, forecast_side, quantity, maximum_price=max_price)
                series_ticker = raw.get("series_ticker")
                series = None
                if isinstance(series_ticker, str) and series_ticker:
                    if series_ticker not in series_cache:
                        operation = "series_fee"
                        series_cache[series_ticker] = _series_metadata(
                            await asyncio.wait_for(client.get_series(series_ticker), timeout=15)
                        )
                    series = series_cache[series_ticker]
                fee = None
                if series:
                    fee = calculate_taker_fee(
                        fee_type=series.get("fee_type"), fee_multiplier=series.get("fee_multiplier"),
                        price=quote.average_price or current.market_implied_probability,
                        quantity=quantity,
                    )
                decision = evaluate_mispricing(
                    context, forecast, fee_dollars=fee,
                    slippage_dollars=quote.slippage_dollars or 0.0,
                    minimum_probability=settings.trading.min_preferred_probability,
                    minimum_edge=settings.trading.min_directional_edge,
                )
                multi_probability_ok = decision.fair_probability >= settings.trading.min_preferred_probability
                multi_edge_ok = (
                    settings.trading.min_directional_edge <= decision.gross_edge
                    <= MAX_UNVALIDATED_PROBABILITY_GAP
                )
                multi_confidence_ok = forecast.calibrated_confidence >= .35
                multi_market_ok = quote.fillable_quantity >= quantity and quote_age <= 5
                multi_final = decision.accepted and multi_market_ok
                final_reason = decision.reason
                if decision.accepted and not multi_market_ok:
                    final_reason = "fresh executable depth cannot fill proposed quantity"
                multi_reasons[final_reason] += 1
                multi_funnel["probability"] += int(multi_probability_ok)
                multi_funnel["edge"] += int(multi_probability_ok and multi_edge_ok)
                multi_funnel["confidence"] += int(multi_probability_ok and multi_edge_ok and multi_confidence_ok)
                multi_funnel["liquidity_freshness"] += int(multi_probability_ok and multi_edge_ok and multi_confidence_ok and multi_market_ok)
                multi_funnel["final"] += int(multi_final)
                multi_accepted += int(multi_final)
                extreme += int(decision.gross_edge > .25)
                consistency = "not_available"
                if context.related_probabilities:
                    consistency = "related_market_evidence_present"
                record = {
                    "market": market.market_id, "category": market.category, "phase": phase,
                    "current_decision": "ACCEPT" if current_final else "REJECT",
                    "current_reason": current_final_reason,
                    "new_decision": "ACCEPT" if multi_final else "REJECT",
                    "new_reason": final_reason,
                    "side": decision.side,
                    "fair_probability": decision.fair_probability,
                    "executable_probability": decision.executable_price,
                    "calibrated_confidence": forecast.calibrated_confidence,
                    "signals": [signal.provider for signal in forecast.signals],
                    "independent_sources": len({signal.correlation_group for signal in forecast.signals}),
                    "agreement": forecast.agreement, "disagreement": forecast.disagreement,
                    "freshness_seconds": forecast.data_freshness_seconds,
                    "executable_liquidity": decision.executable_liquidity,
                    "applicable_fee_dollars": decision.fee_dollars,
                    "slippage_dollars": decision.slippage_dollars,
                    "gross_edge": decision.gross_edge,
                    "net_ev_pct": decision.net_ev_pct if math.isfinite(decision.net_ev_pct) else None,
                    "net_ev_dollars": decision.net_ev_dollars if math.isfinite(decision.net_ev_dollars) else None,
                    "anomaly": decision.gross_edge > MAX_UNVALIDATED_PROBABILITY_GAP,
                    "consistency_check": consistency,
                }
                records.append(record)
                if current_final != multi_final:
                    differences.append(record)
                evaluated += 1
            except asyncio.TimeoutError:
                failures += 1
                failure_reasons[f"{operation}_timeout"] += 1
                continue
            except Exception:
                failures += 1
                failure_reasons[f"{operation}_failure"] += 1
                continue
        print("READ_ONLY_MULTI_SIGNAL_COMPARISON")
        print(f"ENVIRONMENT={environment}")
        print(f"MARKETS_DISCOVERED={discovery.stats.markets_discovered}")
        print(f"MARKETS_WITHIN_72H={discovery.stats.markets_within_72h}")
        print(f"MARKETS_ATTEMPTED={attempted}")
        print(f"MARKETS_EVALUATED={evaluated}")
        print(f"CURRENT_ACCEPTED={current_accepted}")
        print(f"MULTI_SIGNAL_ACCEPTED={multi_accepted}")
        print(f"PHASE_COUNTS={dict(phase_counts)}")
        print(f"CURRENT_FUNNEL={dict(current_funnel)}")
        print(f"MULTI_SIGNAL_FUNNEL={dict(multi_funnel)}")
        print(f"DATA_SOURCE_FAILURES={failures}")
        print(f"DATA_SOURCE_FAILURE_REASONS={dict(failure_reasons)}")
        print(f"MODEL_PROVIDER_CIRCUIT_OPEN={model_provider_unresponsive}")
        print(f"SUSPICIOUS_EXTREME_DISCREPANCIES={extreme}")
        print(f"DECISIONS_CHANGED={len(differences)}")
        print(f"CURRENT_REJECTIONS={dict(current_reasons)}")
        print(f"MULTI_SIGNAL_REJECTIONS={dict(multi_reasons)}")
        print(f"SIGNAL_SOURCES_USED={dict(providers)}")
        old_rejected_new_accepted = [r for r in differences if r["new_decision"] == "ACCEPT"]
        old_accepted_new_rejected = [r for r in differences if r["current_decision"] == "ACCEPT"]
        print(f"OLD_REJECTED_NEW_ACCEPTED={len(old_rejected_new_accepted)}")
        print(f"OLD_ACCEPTED_NEW_REJECTED={len(old_accepted_new_rejected)}")
        ranked = sorted(
            records,
            key=lambda row: row["net_ev_dollars"] if isinstance(row["net_ev_dollars"], (int, float)) else float("-inf"),
            reverse=True,
        )[:30]
        print("TOP_30_NEW_MODEL")
        for rank, record in enumerate(ranked, 1):
            print(f"{rank:02d} " + json.dumps(record, sort_keys=True, allow_nan=False))
        print("EXCHANGE_WRITES=ZERO")
        return 0
    finally:
        await raw_client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 200:
        parser.error("sample size must be between 1 and 200")
    try:
        return asyncio.run(run(args.sample_size))
    except Exception:
        print("READ_ONLY_MULTI_SIGNAL_COMPARISON=BLOCKED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

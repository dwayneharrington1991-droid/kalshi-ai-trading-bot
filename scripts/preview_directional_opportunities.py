#!/usr/bin/env python3
"""GET-only, bounded preview of the revised directional selection policy."""

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv()

from scripts.read_only_validate import ReadOnlyAccountClient, validate_read_only_safety
from src.clients.kalshi_client import KalshiClient
from src.clients.xai_client import XAIClient
from src.config.settings import settings
from src.markets.discovery import MarketDiscovery72h
from src.strategies.directional_policy import (
    classify_sports_phase,
    evaluate_directional_candidate,
)
from src.strategies.portfolio.immediate import _get_fast_ai_prediction
from src.utils.database import Market
from src.utils.market_prices import get_market_prices, is_tradeable_market


def _volume(raw: dict) -> int:
    return int(float(raw.get("volume_fp", 0) or raw.get("volume", 0) or 0))


def _as_market(raw: dict) -> Market:
    resolution = raw["_eligible_resolution_time"]
    yes_bid, yes_ask, no_bid, no_ask = get_market_prices(raw)
    return Market(
        market_id=raw["ticker"], title=raw.get("title", raw["ticker"]),
        yes_price=(yes_bid + yes_ask) / 2, no_price=(no_bid + no_ask) / 2,
        volume=_volume(raw),
        expiration_ts=int(datetime.fromisoformat(resolution.replace("Z", "+00:00")).timestamp()),
        category=raw.get("category", "unknown"), status=raw.get("status", "active"),
        last_updated=datetime.now(),
    )


async def run(max_model_calls: int, limit: int) -> int:
    environment = validate_read_only_safety(os.environ)
    if os.getenv("LIVE_TRADING_ENABLED", "").lower() != "false":
        raise RuntimeError("LIVE_TRADING_ENABLED must be explicitly false")

    raw_client = KalshiClient(environment=environment)
    client = ReadOnlyAccountClient(raw_client, environment)
    model = XAIClient()
    try:
        discovery = await MarketDiscovery72h(client).discover()
        candidates = []
        for raw in discovery.markets:
            if not is_tradeable_market(raw) or _volume(raw) < 100:
                continue
            yes_bid, yes_ask, no_bid, no_ask = get_market_prices(raw)
            favored_ask = max(yes_ask, no_ask)
            favored_spread = (yes_ask - yes_bid) if yes_ask >= no_ask else (no_ask - no_bid)
            if favored_ask < settings.trading.min_preferred_probability:
                continue
            if favored_spread > getattr(settings.trading, "max_bid_ask_spread", .15):
                continue
            candidates.append(raw)
        candidates.sort(key=lambda raw: (_volume(raw), max(get_market_prices(raw)[1], get_market_prices(raw)[3])), reverse=True)

        results = []
        for raw in candidates[:max_model_calls]:
            market = _as_market(raw)
            yes_bid, yes_ask, no_bid, no_ask = get_market_prices(raw)
            predicted, confidence = await _get_fast_ai_prediction(market, model, yes_ask)
            if predicted is None:
                continue
            phase = classify_sports_phase(raw, market.category)
            result = evaluate_directional_candidate(
                market_id=market.market_id, predicted_yes_probability=predicted,
                yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
                min_probability=settings.trading.min_preferred_probability,
                max_preferred_probability=settings.trading.max_preferred_probability,
                min_edge=settings.trading.min_directional_edge,
                fee_estimate=settings.trading.directional_fee_estimate,
                slippage_estimate=settings.trading.directional_slippage_estimate,
                sports_phase=phase,
            )
            if result.accepted:
                results.append((result, confidence, market.title, market.category))
        results.sort(key=lambda item: item[0].ranking_score, reverse=True)

        stats = discovery.stats.as_dict()
        print("READ_ONLY_DIRECTIONAL_PREVIEW")
        print(f"ENVIRONMENT={environment}")
        print(f"MARKETS_WITHIN_72H={stats['markets_within_72h']}")
        print(f"SPORTS_WITHIN_72H={stats['sports_markets_within_72h']}")
        print(f"MODELED_CANDIDATES={min(len(candidates), max_model_calls)}")
        print(f"QUALIFYING_CANDIDATES={len(results)}")
        print("RANK | MARKET | CATEGORY | PHASE | SIDE | PRICE | MODEL | EDGE | NET | CONFIDENCE")
        for rank, (result, confidence, _, category) in enumerate(results[:limit], 1):
            print(
                f"{rank:02d} | {result.market_id} | {category} | {result.sports_phase} | "
                f"{result.side} | {result.market_implied_probability:.1%} | "
                f"{result.estimated_probability:.1%} | {result.gross_edge:.1%} | "
                f"{result.estimated_net_return:.1%} | {confidence:.1%}"
            )
        print("EXCHANGE_WRITES=IMPOSSIBLE")
        print("LIVE_TRADING=DISABLED")
        return 0
    finally:
        await model.close()
        await raw_client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-model-calls", type=int, default=60)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.max_model_calls <= 200 or not 1 <= args.limit <= 20:
        parser.error("limits must be bounded (model calls 1-200, output 1-20)")
    try:
        return asyncio.run(run(args.max_model_calls, args.limit))
    except Exception:
        print("READ_ONLY_DIRECTIONAL_PREVIEW=BLOCKED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

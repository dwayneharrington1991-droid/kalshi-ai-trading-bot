"""Read-only runtime adapter for the dedicated KXBTC15M strategy.

This module deliberately owns no order path.  It obtains production market data
and feeds it into the existing KXBTC15M discovery and signal-engine classes.
REST order-book snapshots are logged for price visibility, but are never
misrepresented as a fresh sequenced WebSocket book; the existing engine then
fails closed until the WebSocket/reference integration provides the required
authoritative inputs.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from src.signals.fees import calculate_taker_fee
from src.strategies.kxbtc15m import (
    KXBTC15MDiscovery,
    KXBTC15MSettings,
    KXBTC15MSignalEngine,
    ReferencePrice,
    ReconstructedOrderBook,
    parse_contract_metadata,
)


@dataclass(frozen=True)
class KXBTC15MShadowResult:
    ticker: Optional[str]
    action: str
    reason: str
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    p_up: Optional[float] = None
    p_down: Optional[float] = None
    gross_edge: Optional[float] = None
    net_edge: Optional[float] = None
    confidence: Optional[float] = None
    liquidity: float = 0.0
    hypothetical_contracts: int = 0
    would_submit: bool = False


def _levels(raw: Any) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        return []
    levels: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            return []
        try:
            price, quantity = float(level[0]), float(level[1])
        except (TypeError, ValueError):
            return []
        if not (0 < price < 1) or quantity < 0:
            return []
        levels.append((price, quantity))
    return levels


def _best_prices(orderbook: Any) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return executable YES/NO bid/ask from Kalshi's opposing-side bids."""
    payload = orderbook.get("orderbook_fp", orderbook) if isinstance(orderbook, dict) else {}
    yes, no = _levels(payload.get("yes_dollars", [])), _levels(payload.get("no_dollars", []))
    yes_bid = max((price for price, _ in yes), default=None)
    no_bid = max((price for price, _ in no), default=None)
    yes_ask = 1 - no_bid if no_bid is not None else None
    no_ask = 1 - yes_bid if yes_bid is not None else None
    return yes_bid, yes_ask, no_bid, no_ask


async def run_kxbtc15m_shadow_cycle(
    client: Any,
    *,
    strategy_settings: KXBTC15MSettings,
    max_market_risk: float,
    logger: Optional[logging.Logger] = None,
) -> KXBTC15MShadowResult:
    """Run one GET-only KXBTC15M cycle and emit a reasoned shadow outcome."""
    log = logger or logging.getLogger(__name__)
    # The account read is intentionally retained even in shadow mode: a live
    # transition must never be considered from an unauthenticated/unfunded run.
    try:
        await client.get_balance()
    except Exception:
        result = KXBTC15MShadowResult(None, "NO_TRADE", "balance verification failed")
        log.warning("KXBTC15M_SHADOW_DECISION %s", asdict(result))
        return result

    try:
        market = await KXBTC15MDiscovery(client).active_contract()
        if market is None:
            result = KXBTC15MShadowResult(None, "NO_TRADE", "no valid active KXBTC15M market")
            log.info("KXBTC15M_SHADOW_DECISION %s", asdict(result))
            return result
        series_response = await client.get_series("KXBTC15M")
        series = series_response.get("series", series_response) if isinstance(series_response, dict) else {}
        metadata = parse_contract_metadata(market, series, scoped_series_ticker="KXBTC15M")
        if metadata is None:
            result = KXBTC15MShadowResult(market.get("ticker"), "NO_TRADE", "ambiguous KXBTC15M market metadata")
            log.warning("KXBTC15M_SHADOW_DECISION %s", asdict(result))
            return result
        orderbook = await client.get_orderbook(metadata.ticker)
    except Exception as exc:
        result = KXBTC15MShadowResult(None, "NO_TRADE", f"market-data retrieval failed: {type(exc).__name__}")
        log.warning("KXBTC15M_SHADOW_DECISION %s", asdict(result))
        return result

    yes_bid, yes_ask, no_bid, no_ask = _best_prices(orderbook)
    if None in (yes_bid, yes_ask, no_bid, no_ask):
        result = KXBTC15MShadowResult(metadata.ticker, "NO_TRADE", "valid executable bid/ask unavailable")
        log.info("KXBTC15M_SHADOW_DECISION %s", asdict(result))
        return result

    # REST has no sequence number.  Do not turn it into a valid WebSocket book:
    # the signal engine must reject until a sequenced, fresh stream is present.
    book = ReconstructedOrderBook()
    fee_type = str(series.get("fee_type", ""))
    try:
        calculated_fee = calculate_taker_fee(
            fee_type=fee_type,
            fee_multiplier=float(series.get("fee_multiplier", 1)),
            price=min(yes_ask, no_ask), quantity=1,
        )
        fee_rate = calculated_fee if calculated_fee is not None else 1.0
    except (TypeError, ValueError):
        fee_rate = 1.0  # conservative fallback; later gates remain fail-closed
    decision = KXBTC15MSignalEngine(strategy_settings).evaluate(
        market=market,
        target_price=metadata.target_price,
        reference=ReferencePrice(0.0, time.monotonic(), "unavailable", None),
        price_history=[], book=book,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        fee_rate=fee_rate, slippage_rate=0.0,
    )
    price = yes_ask if decision.side == "YES" else no_ask if decision.side == "NO" else None
    contracts = int(max_market_risk // price) if price and price > 0 else 0
    result = KXBTC15MShadowResult(
        metadata.ticker, decision.action, decision.reason,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        p_up=decision.p_up, p_down=decision.p_down,
        gross_edge=decision.gross_edge, net_edge=decision.net_edge,
        confidence=decision.confidence, liquidity=decision.liquidity,
        hypothetical_contracts=contracts,
        # Shadow mode never submits, even if a future data source yields TRADE.
        would_submit=False,
    )
    log.info("KXBTC15M_SHADOW_DECISION %s", asdict(result))
    return result

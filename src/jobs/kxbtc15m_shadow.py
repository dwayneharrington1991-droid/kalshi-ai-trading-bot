"""Read-only runtime adapter for the dedicated KXBTC15M strategy.

This module deliberately owns no order path.  It obtains production market data
and feeds it into the existing KXBTC15M discovery and signal-engine classes.
REST order-book snapshots are logged for price visibility, but are never
misrepresented as a fresh sequenced WebSocket book; the existing engine then
fails closed until the WebSocket/reference integration provides the required
authoritative inputs.
"""

from __future__ import annotations

import asyncio
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


def _default_ws_factory(**kwargs: Any) -> Any:
    # Keep the optional websocket dependency out of REST-only/test imports while
    # still using the repository's single production WebSocket implementation.
    from src.clients.kalshi_ws import KalshiWebSocket

    return KalshiWebSocket(**kwargs)


class KXBTC15MBookStream:
    """Persistent, fail-closed view of one active KXBTC15M WebSocket book."""

    def __init__(
        self,
        *,
        ws_factory: Any = _default_ws_factory,
        logger: Optional[logging.Logger] = None,
        max_age_seconds: float = 5.0,
    ) -> None:
        self.ws_factory = ws_factory
        self.log = logger or logging.getLogger(__name__)
        self.ticker: Optional[str] = None
        self.book = ReconstructedOrderBook()
        self.ws: Any = None
        self.run_task: Optional[asyncio.Task[Any]] = None
        self.monitor_task: Optional[asyncio.Task[Any]] = None
        self.snapshot_event = asyncio.Event()
        self.resync_reason: Optional[str] = "not_subscribed"
        self.max_age_seconds = max_age_seconds
        self._api_key: Optional[str] = None
        self._private_key_path: Optional[str] = None
        self._lock = asyncio.Lock()

    def _diagnostic(self, event: str) -> None:
        now = time.monotonic()
        age_ms = None if self.book.received_at is None else max(0.0, (now - self.book.received_at) * 1000)
        connected = bool(self.ws and self.ws.is_connected)
        self.log.info(
            "KXBTC15M_WS event=%s connected=%s ticker=%s snapshot_received=%s "
            "sequence=%s book_age_ms=%s sequence_valid=%s stale=%s resync_reason=%s",
            event, connected, self.ticker, self.snapshot_event.is_set(), self.book.sequence,
            None if age_ms is None else round(age_ms, 1), self.book.valid,
            not self.book.fresh(self.max_age_seconds, now=now), self.resync_reason,
        )

    def _invalidate(self, reason: str) -> None:
        self.book.valid = False
        self.snapshot_event.clear()
        self.resync_reason = reason
        self._diagnostic("invalidated")

    async def _on_orderbook(self, message: dict[str, Any]) -> None:
        payload = message.get("msg", {}) if isinstance(message, dict) else {}
        message_ticker = payload.get("market_ticker") or payload.get("ticker")
        if message_ticker and message_ticker != self.ticker:
            return
        message_type = message.get("type")
        if message_type == "orderbook_snapshot":
            if self.book.apply_snapshot(message, unified_yes_price=True):
                self.resync_reason = None
                self.snapshot_event.set()
                self._diagnostic("snapshot")
            else:
                self._invalidate("invalid_snapshot")
            return
        if message_type == "orderbook_delta":
            if self.book.apply_delta(message):
                self._diagnostic("delta")
            else:
                self._invalidate("sequence_gap_or_invalid_delta")
                asyncio.create_task(self._resync("sequence_gap_or_invalid_delta"))

    async def _monitor_connection(self) -> None:
        was_connected = True
        try:
            while True:
                connected = bool(self.ws and self.ws.is_connected)
                if was_connected and not connected:
                    self._invalidate("websocket_disconnected")
                    self._diagnostic("disconnected")
                elif not was_connected and connected:
                    # KalshiWebSocket resubscribes automatically, but the old
                    # book stays invalid until the new subscription snapshot.
                    self._invalidate("reconnected_waiting_for_snapshot")
                    self._diagnostic("reconnected")
                was_connected = connected
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return

    async def _stop_locked(self, reason: str) -> None:
        if self.ws is not None:
            await self.ws.close()
        for task in (self.run_task, self.monitor_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self.run_task, self.monitor_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.run_task = self.monitor_task = None
        self.ws = None
        self._invalidate(reason)

    async def _start_locked(self, ticker: str, *, api_key: str, private_key_path: str) -> None:
        self.ticker = ticker
        self._api_key, self._private_key_path = api_key, private_key_path
        self.book = ReconstructedOrderBook()
        self.snapshot_event = asyncio.Event()
        self.resync_reason = "waiting_for_snapshot"
        self.ws = self.ws_factory(
            api_key=api_key, private_key_path=private_key_path, publish_to_event_bus=False,
        )
        self.ws.on_orderbook(self._on_orderbook)
        await self.ws.connect()
        self._diagnostic("connected")
        await self.ws.subscribe([ticker], ["orderbook_delta"], use_yes_price=True)
        self._diagnostic("subscribed")
        self.run_task = asyncio.create_task(self.ws.run())
        self.monitor_task = asyncio.create_task(self._monitor_connection())

    async def _resync(self, reason: str) -> None:
        async with self._lock:
            if not self.ticker or not self._api_key or not self._private_key_path:
                return
            ticker, api_key, private_key_path = self.ticker, self._api_key, self._private_key_path
            await self._stop_locked(reason)
            try:
                await self._start_locked(ticker, api_key=api_key, private_key_path=private_key_path)
            except Exception as exc:
                self._invalidate(f"resync_failed:{type(exc).__name__}")

    async def ensure_subscription(self, ticker: str, *, api_key: str, private_key_path: str) -> None:
        async with self._lock:
            if self.ticker == ticker and self.ws is not None and self.run_task is not None and not self.run_task.done():
                return
            if self.ws is not None:
                await self._stop_locked("contract_rollover")
            await self._start_locked(ticker, api_key=api_key, private_key_path=private_key_path)

    async def wait_for_snapshot(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.snapshot_event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self._invalidate("snapshot_timeout")
            return False
        return self.book.valid

    async def close(self) -> None:
        async with self._lock:
            await self._stop_locked("closed")


_SHADOW_STREAM: Optional[KXBTC15MBookStream] = None
_SHADOW_STREAM_LOOP: Optional[asyncio.AbstractEventLoop] = None


def _stream_for_current_loop(logger: logging.Logger) -> KXBTC15MBookStream:
    global _SHADOW_STREAM, _SHADOW_STREAM_LOOP
    loop = asyncio.get_running_loop()
    if _SHADOW_STREAM is None or _SHADOW_STREAM_LOOP is not loop:
        _SHADOW_STREAM = KXBTC15MBookStream(logger=logger)
        _SHADOW_STREAM_LOOP = loop
    return _SHADOW_STREAM


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
    book_stream: Optional[KXBTC15MBookStream] = None,
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

    stream = book_stream or _stream_for_current_loop(log)
    stream.max_age_seconds = strategy_settings.max_ws_age_seconds
    try:
        await stream.ensure_subscription(
            metadata.ticker,
            api_key=str(client.api_key),
            private_key_path=str(client.private_key_path),
        )
        await stream.wait_for_snapshot(3.0)
    except Exception as exc:
        stream._invalidate(f"websocket_setup_failed:{type(exc).__name__}")
    book = stream.book
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

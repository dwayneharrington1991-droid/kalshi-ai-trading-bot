import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.jobs.kxbtc15m_shadow import KXBTC15MBookStream, run_kxbtc15m_shadow_cycle
from src.strategies.kxbtc15m import KXBTC15MSettings


def active_market():
    return {
        "ticker": "KXBTC15M-TEST", "status": "active",
        "open_time": datetime.now(timezone.utc).isoformat(),
        "close_time": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        "floor_strike": 100000.0, "cap_strike": None,
        "strike_type": "greater_or_equal", "event_ticker": "KXBTC15M-EVENT",
        "rules_primary": "Official source.", "rules_secondary": "Official close rule.",
    }


class ReadOnlyClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.api_key = "test-key-id"
        self.private_key_path = "test-private-key.pem"

    async def get_balance(self):
        self.calls.append("balance")
        return {"balance": 1}

    async def get_markets(self, **kwargs):
        self.calls.append("markets")
        assert kwargs["series_ticker"] == "KXBTC15M"
        return {"markets": [active_market()], "cursor": None}

    async def get_series(self, ticker):
        self.calls.append("series")
        assert ticker == "KXBTC15M"
        return {"series": {"fee_type": "quadratic", "fee_multiplier": 1,
                           "settlement_sources": [{"name": "CF Benchmarks"}]}}

    async def get_orderbook(self, ticker):
        self.calls.append("orderbook")
        assert ticker == "KXBTC15M-TEST"
        return {"orderbook_fp": {"yes_dollars": [["0.14", "3"]],
                                  "no_dollars": [["0.85", "4"]]}}


@pytest.mark.asyncio
class FakeWebSocket:
    def __init__(self, factory, **kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.state = "disconnected"
        self.orderbook_callback = None
        self.subscriptions = []
        self.closed = False
        self._stopped = asyncio.Event()

    @property
    def is_connected(self):
        return self.state == "connected"

    def on_orderbook(self, callback):
        self.orderbook_callback = callback

    async def connect(self):
        self.state = "connected"

    async def subscribe(self, market_tickers, channels, **kwargs):
        self.subscriptions.append((market_tickers, channels, kwargs))
        messages = self.factory.messages_by_instance[len(self.factory.instances) - 1]
        for message in messages:
            await self.orderbook_callback(message)

    async def run(self):
        await self._stopped.wait()

    async def close(self):
        self.closed = True
        self.state = "disconnected"
        self._stopped.set()


class FakeWebSocketFactory:
    def __init__(self, *messages_by_instance):
        self.messages_by_instance = list(messages_by_instance) or [[]]
        self.instances = []

    def __call__(self, **kwargs):
        if len(self.instances) >= len(self.messages_by_instance):
            self.messages_by_instance.append([])
        websocket = FakeWebSocket(self, **kwargs)
        self.instances.append(websocket)
        return websocket


def snapshot(ticker="KXBTC15M-TEST", sequence=10):
    return {
        "type": "orderbook_snapshot",
        "seq": sequence,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.14", "3"]],
            "no_dollars_fp": [["0.15", "4"]],
        },
    }


def delta(sequence, *, ticker="KXBTC15M-TEST", side="yes", price="0.14", change="1"):
    return {
        "type": "orderbook_delta",
        "seq": sequence,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": price,
            "delta_fp": change,
        },
    }


@pytest.mark.asyncio
async def test_runtime_routes_active_contract_through_prices_and_fresh_ws_book():
    client = ReadOnlyClient()
    stream = KXBTC15MBookStream(ws_factory=FakeWebSocketFactory([snapshot()]))
    try:
        result = await run_kxbtc15m_shadow_cycle(
            client, strategy_settings=KXBTC15MSettings(enabled=True), max_market_risk=1,
            book_stream=stream,
        )
        assert client.calls == ["balance", "markets", "series", "orderbook"]
        assert result.ticker == "KXBTC15M-TEST"
        assert result.yes_bid == pytest.approx(.14)
        assert result.yes_ask == pytest.approx(.15)
        assert result.no_bid == pytest.approx(.85)
        assert result.no_ask == pytest.approx(.86)
        assert result.action == "NO_TRADE"
        assert result.reason == "Kalshi-compatible reference price unavailable"
        assert not result.would_submit
        assert result.hypothetical_contracts == 0
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_accepts_fresh_snapshot_and_sequential_deltas():
    factory = FakeWebSocketFactory([snapshot()])
    stream = KXBTC15MBookStream(ws_factory=factory)
    try:
        await stream.ensure_subscription("KXBTC15M-TEST", api_key="key", private_key_path="key.pem")
        assert await stream.wait_for_snapshot(.1)
        assert stream.book.valid
        assert stream.book.sequence == 10
        await stream._on_orderbook(delta(11))
        assert stream.book.valid
        assert stream.book.sequence == 11
        assert stream.book.yes_bids[.14] == pytest.approx(4)
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_marks_old_book_stale():
    stream = KXBTC15MBookStream(ws_factory=FakeWebSocketFactory([snapshot()]), max_age_seconds=.001)
    try:
        await stream.ensure_subscription("KXBTC15M-TEST", api_key="key", private_key_path="key.pem")
        assert await stream.wait_for_snapshot(.1)
        stream.book.received_at -= 1
        assert not stream.book.fresh(stream.max_age_seconds)
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_sequence_gap_fails_closed_and_resubscribes():
    factory = FakeWebSocketFactory([snapshot()], [])
    stream = KXBTC15MBookStream(ws_factory=factory)
    try:
        await stream.ensure_subscription("KXBTC15M-TEST", api_key="key", private_key_path="key.pem")
        assert await stream.wait_for_snapshot(.1)
        await stream._on_orderbook(delta(12))
        await asyncio.sleep(.02)
        assert len(factory.instances) == 2
        assert not stream.book.valid
        assert stream.resync_reason == "waiting_for_snapshot"
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_reconnect_requires_a_new_snapshot():
    factory = FakeWebSocketFactory([snapshot()])
    stream = KXBTC15MBookStream(ws_factory=factory)
    try:
        await stream.ensure_subscription("KXBTC15M-TEST", api_key="key", private_key_path="key.pem")
        websocket = factory.instances[0]
        websocket.state = "disconnected"
        await asyncio.sleep(.12)
        assert not stream.book.valid
        assert stream.resync_reason == "websocket_disconnected"
        websocket.state = "connected"
        await asyncio.sleep(.12)
        assert not stream.book.valid
        assert stream.resync_reason == "reconnected_waiting_for_snapshot"
        await stream._on_orderbook(snapshot(sequence=20))
        assert stream.book.valid
        assert stream.book.sequence == 20
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_rollover_replaces_subscription_and_requires_snapshot():
    factory = FakeWebSocketFactory([snapshot("KXBTC15M-ONE")], [snapshot("KXBTC15M-TWO", 1)])
    stream = KXBTC15MBookStream(ws_factory=factory)
    try:
        await stream.ensure_subscription("KXBTC15M-ONE", api_key="key", private_key_path="key.pem")
        assert await stream.wait_for_snapshot(.1)
        first = factory.instances[0]
        await stream.ensure_subscription("KXBTC15M-TWO", api_key="key", private_key_path="key.pem")
        assert first.closed
        assert await stream.wait_for_snapshot(.1)
        assert stream.ticker == "KXBTC15M-TWO"
        assert stream.book.sequence == 1
        assert factory.instances[1].subscriptions[0][0] == ["KXBTC15M-TWO"]
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_book_stream_without_websocket_data_fails_closed():
    stream = KXBTC15MBookStream(ws_factory=FakeWebSocketFactory([]))
    try:
        await stream.ensure_subscription("KXBTC15M-TEST", api_key="key", private_key_path="key.pem")
        assert not await stream.wait_for_snapshot(.01)
        assert not stream.book.valid
        assert stream.resync_reason == "snapshot_timeout"
    finally:
        await stream.close()


@pytest.mark.asyncio
async def test_runtime_does_not_continue_when_balance_cannot_be_verified():
    class BalanceFailureClient(ReadOnlyClient):
        async def get_balance(self):
            raise RuntimeError("unavailable")

    result = await run_kxbtc15m_shadow_cycle(
        BalanceFailureClient(), strategy_settings=KXBTC15MSettings(enabled=True), max_market_risk=1,
    )
    assert result.action == "NO_TRADE"
    assert result.reason == "balance verification failed"
    assert not result.would_submit

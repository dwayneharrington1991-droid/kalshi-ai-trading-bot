"""Credential-free BTC streams; no account client or execution capability.

Coinbase Advanced Trade public ticker/market_trades/heartbeats:
https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/websocket/websocket-channels
The market_trades `side` is the MAKER side, so aggressor direction is inverted.
Kraken's existing checksum/sequence-verified sockets are sampled through an
adapter, avoiding duplicate subscriptions. Its older collector does not retain
socket receipt times; adapter sample times are explicitly identified as such.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import json
import random
import threading
from time import monotonic, perf_counter

from .model import number, timestamp

COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
MAX_AGE = 5.0


def utcnow():
    return datetime.now(timezone.utc)


def numeric(value, low=0, high=1e15):
    if isinstance(value, bool):
        raise ValueError("Boolean market value")
    return number(float(value), low, high)


class StreamHub:
    """Latest-value publication plus a bounded observation queue.

    Consumers wait at most 500ms, drain observations into the independent rolling
    intelligence buffer, and publish only meaningful new market data. Heartbeats
    do not refresh prices. No network starts when autostart=False (offline tests).
    """

    def __init__(self, *, order_flow=None, autostart=True, connector=None, max_pending=2048):
        if not 1 <= max_pending <= 100000:
            raise ValueError("Bounded pending capacity required")
        self.order_flow = order_flow
        self.connector = connector
        self.condition = threading.Condition(threading.RLock())
        self.stop = threading.Event()
        self.sequence = 0
        self.pending = deque(maxlen=max_pending)
        self.feeds = {}
        self.sessions = {}
        self.channel_sequences = {}
        self.seen_ids = set()
        self.trade_ids = deque(maxlen=10000)
        self.kraken_ids = {}
        self.kraken_sessions = {}
        self.kraken_book_at = None
        self.socket = None
        self.thread = None
        self.metrics = {
            "connection_attempts": 0,
            "reconnects": 0,
            "received_messages": 0,
            "accepted_observations": 0,
            "rejected_messages": 0,
            "missing_updates": 0,
            "queue_dropped": 0,
            "pending_high_water": 0,
            "historical_snapshot_records_excluded": 0,
            "last_processing_ms": None,
            "last_transport_ping_ms": None,
            "last_heartbeat_received_at": None,
            "last_error": None,
            "clock_status": "UNVERIFIED — event/receipt difference includes clock offset, publication and transport delay",
        }
        self.dropped_since_drain = 0
        self._last_monotonic = None
        self._last_utc = None
        self.begin_session("coinbase")
        if autostart:
            self.thread = threading.Thread(
                target=self.run, daemon=True, name="public-coinbase-stream"
            )
            self.thread.start()

    def begin_session(self, venue):
        with self.condition:
            self.sessions[venue] = self.sessions.get(venue, 0) + 1
            if venue == "coinbase":
                self.channel_sequences.clear()
                self.seen_ids.clear()
                self.trade_ids.clear()
                self._last_utc = self._last_monotonic = None
                previous = self.feeds.get("Coinbase BTC/USD stream", {})
                self.feeds["Coinbase BTC/USD stream"] = {
                    **previous,
                    "connected": False,
                    "session_id": self.sessions[venue],
                    "status_reason": "Waiting for a fresh post-connect market sample",
                    "bid": None,
                    "ask": None,
                    "quote_received_at": None,
                }
            self.sequence += 1
            self.condition.notify_all()

    def disconnected(self, error):
        with self.condition:
            state = self.feeds.setdefault("Coinbase BTC/USD stream", {})
            state["connected"] = False
            state["status_reason"] = "Public stream disconnected; waiting for post-reconnect data"
            self.metrics["last_error"] = (
                type(error).__name__ if isinstance(error, Exception) else str(error)
            )
            self.sequence += 1
            self.condition.notify_all()

    def _observe(self, observation):
        if len(self.pending) == self.pending.maxlen:
            self.metrics["queue_dropped"] += 1
            self.dropped_since_drain += 1
        self.pending.append(observation)
        self.metrics["accepted_observations"] += 1
        self.metrics["pending_high_water"] = max(
            self.metrics["pending_high_water"], len(self.pending)
        )
        self.sequence += 1
        self.condition.notify_all()

    def _clock_check(self, received_at, received_mono):
        if self._last_utc is not None and received_mono >= self._last_monotonic:
            wall = (received_at - self._last_utc).total_seconds()
            elapsed = received_mono - self._last_monotonic
            if abs(wall - elapsed) > 1:
                self.metrics[
                    "clock_status"
                ] = "CLOCK DISCONTINUITY — UTC and monotonic elapsed times differ by more than one second"
                raise ValueError("Clock discontinuity; fresh session required")
        self._last_utc, self._last_monotonic = received_at, received_mono

    def process_coinbase(self, message, received_at=None, received_monotonic=None):
        """Parse a public message; source times never replace local receipt times."""
        started = perf_counter()
        received_at = received_at or utcnow()
        received_mono = monotonic() if received_monotonic is None else received_monotonic
        try:
            data = json.loads(message) if isinstance(message, (str, bytes)) else message
            with self.condition:
                self._clock_check(received_at, received_mono)
                self.metrics["received_messages"] += 1
                if data.get("type") == "error":
                    raise ValueError("Public subscription rejected")
                channel = data.get("channel")
                if channel not in {"ticker", "market_trades", "heartbeats"}:
                    return False
                seq = data.get("sequence_num")
                if seq is not None:
                    if type(seq) is not int or seq < 0:
                        raise ValueError("Invalid source sequence")
                    previous = self.channel_sequences.get(channel)
                    if previous is not None:
                        if seq == previous:
                            return False
                        if seq < previous:
                            raise ValueError("Out-of-order source sequence")
                        if seq != previous + 1:
                            self.metrics["missing_updates"] += seq - previous - 1
                            raise ValueError("Source sequence gap; reconnect required")
                    self.channel_sequences[channel] = seq
                if channel == "heartbeats":
                    self.metrics["last_heartbeat_received_at"] = received_at.isoformat()
                    return False
                accepted = False
                for event in data.get("events", []):
                    if channel == "ticker":
                        for ticker in event.get("tickers", []):
                            if ticker.get("product_id") != "BTC-USD":
                                continue
                            at = timestamp(data["timestamp"]) if data.get("timestamp") else None
                            observation = self._coinbase_quote(ticker, at, received_at)
                            self._publish_coinbase(observation)
                            accepted = True
                    else:
                        for trade in event.get("trades", []):
                            if trade.get("product_id") != "BTC-USD":
                                continue
                            identifier = str(trade["trade_id"])
                            if identifier in self.seen_ids:
                                continue
                            at = timestamp(trade["time"])
                            # Coinbase's initial snapshot is a list of recent
                            # historical executions. It is not a live update;
                            # exclude old members rather than reconnecting in a
                            # loop or assigning them a fresh event timestamp.
                            if (
                                event.get("type") == "snapshot"
                                and (received_at - at).total_seconds() > MAX_AGE
                            ):
                                self.metrics["historical_snapshot_records_excluded"] += 1
                                continue
                            maker = trade.get("side")
                            if maker not in {"BUY", "SELL"}:
                                raise ValueError("Unsupported maker direction")
                            self._valid_event(at, received_at)
                            observation = {
                                "source": "Coinbase BTC/USD stream",
                                "kind": "trade",
                                "price": numeric(trade["price"], 1e-6),
                                "volume": numeric(trade["size"], 1e-12),
                                "aggressor": "sell" if maker == "BUY" else "buy",
                                "maker_side": maker,
                                "trade_id": identifier,
                                "event_at": at.isoformat(),
                                "received_at": received_at.isoformat(),
                                "event_time_basis": "exchange trade event timestamp",
                                "receipt_basis": "actual local socket receipt",
                                "session_id": self.sessions["coinbase"],
                            }
                            if len(self.trade_ids) == self.trade_ids.maxlen:
                                self.seen_ids.discard(self.trade_ids[0])
                            self.trade_ids.append(identifier)
                            self.seen_ids.add(identifier)
                            self._publish_coinbase(observation)
                            accepted = True
                return accepted
        except (ValueError, KeyError, TypeError, OverflowError, AttributeError) as exc:
            with self.condition:
                self.metrics["rejected_messages"] += 1
            self.disconnected(exc)
            raise ValueError("Invalid or incomplete public stream message") from None
        finally:
            with self.condition:
                self.metrics["last_processing_ms"] = round((perf_counter() - started) * 1000, 3)

    @staticmethod
    def _valid_event(at, received):
        if at is not None and not 0 <= (received - at).total_seconds() <= MAX_AGE:
            raise ValueError("Stale/future exchange event or clock mismatch")

    def _coinbase_quote(self, row, at, received):
        self._valid_event(at, received)
        price = numeric(row["price"], 1e-6)
        bid = numeric(row["best_bid"], 1e-6) if row.get("best_bid") is not None else None
        ask = numeric(row["best_ask"], 1e-6) if row.get("best_ask") is not None else None
        if bid is not None and ask is not None and (bid > ask or (ask - bid) / bid > 0.02):
            raise ValueError("Crossed or incompatible spot quote")
        return {
            "source": "Coinbase BTC/USD stream",
            "kind": "quote",
            "price": price,
            "bid": bid,
            "ask": ask,
            "bid_depth": numeric(row["best_bid_quantity"])
            if row.get("best_bid_quantity") is not None
            else None,
            "ask_depth": numeric(row["best_ask_quantity"])
            if row.get("best_ask_quantity") is not None
            else None,
            "volume": None,
            "aggressor": None,
            "event_at": at.isoformat() if at else None,
            "received_at": received.isoformat(),
            "event_time_basis": "exchange message publication timestamp; not last-trade time"
            if at
            else "unavailable",
            "receipt_basis": "actual local socket receipt",
            "session_id": self.sessions["coinbase"],
        }

    def _publish_coinbase(self, observation):
        old = self.feeds.get("Coinbase BTC/USD stream", {})
        older_event = (
            old.get("event_time") is not None
            and observation.get("event_at") is not None
            and timestamp(observation["event_at"]) < timestamp(old["event_time"])
        )
        # Preserve quote depth separately; a trade never refreshes old bid/ask timestamps.
        feed = {
            **old,
            "connected": True,
            "status_reason": None,
            "price": old.get("price") if older_event else observation["price"],
            "observed_at": observation["received_at"],
            "event_time": observation["event_at"],
            "event_time_basis": observation["event_time_basis"],
            "receipt_basis": observation["receipt_basis"],
            "source": "Coinbase BTC/USD public WebSocket (proxy, NOT settlement index)",
            "session_id": observation["session_id"],
        }
        if older_event:
            # A delayed trade on a different channel remains a real historical
            # observation but must not rewind the latest reference or its age.
            feed.update(
                observed_at=old["observed_at"],
                event_time=old["event_time"],
                event_time_basis=old.get("event_time_basis"),
            )
        if observation["kind"] == "quote":
            feed.update(
                bid=observation["bid"],
                ask=observation["ask"],
                bid_size=observation["bid_depth"],
                ask_size=observation["ask_depth"],
                quote_received_at=observation["received_at"],
            )
        self.feeds["Coinbase BTC/USD stream"] = feed
        self.metrics["last_error"] = None
        self._observe(observation)

    def _sample_kraken(self, now):
        if self.order_flow is None:
            return
        observations = []
        with self.order_flow.lock:
            for venue, flow, label in (
                ("spot", self.order_flow.spot, "Kraken BTC/USD stream"),
                ("perpetual", self.order_flow.perp, "Kraken futures stream"),
            ):
                session = flow.started.isoformat() if flow.started else None
                if session != self.kraken_sessions.get(venue):
                    self.kraken_sessions[venue] = session
                    self.kraken_ids.pop(venue, None)
                    if label in self.feeds:
                        self.feeds[label] = {**self.feeds[label], "connected": False}
                if not session:
                    continue
                last_id = self.kraken_ids.get(venue)
                unseen = []
                for trade in reversed(flow.trades):
                    if trade[0] == last_id:
                        break
                    unseen.append(trade)
                    if len(unseen) >= self.pending.maxlen:
                        break
                for trade in reversed(unseen):
                    if not 0 <= (now - trade[1]).total_seconds() <= MAX_AGE:
                        continue
                    observations.append(
                        {
                            "source": label,
                            "kind": "trade",
                            "price": trade[2],
                            "volume": trade[3],
                            "aggressor": trade[4],
                            "trade_id": trade[0],
                            "event_at": trade[1].isoformat(),
                            "received_at": now.isoformat(),
                            "event_time_basis": "exchange trade event timestamp",
                            "receipt_basis": "adapter sample time; original socket receipt timestamp unavailable",
                            "session_id": session,
                            "quantity_unit": "BTC"
                            if venue == "spot"
                            else "provider raw PF_XBTUSD quantity; not pooled with spot",
                        }
                    )
                if flow.trades:
                    self.kraken_ids[venue] = flow.trades[-1][0]
            book = self.order_flow.book
            if (
                book.ready
                and book.at
                and book.at != self.kraken_book_at
                and 0 <= (now - book.at).total_seconds() <= MAX_AGE
            ):
                bids, asks = sorted(book.bids, reverse=True)[:20], sorted(book.asks)[:20]
                if bids and asks:
                    bid, ask = float(bids[0]), float(asks[0])
                    observations.append(
                        {
                            "source": "Kraken BTC/USD stream",
                            "kind": "quote",
                            "price": (bid + ask) / 2,
                            "bid": bid,
                            "ask": ask,
                            "bid_depth": float(sum(book.bids[p] for p in bids)),
                            "ask_depth": float(sum(book.asks[p] for p in asks)),
                            "volume": None,
                            "aggressor": None,
                            "event_at": book.at.isoformat(),
                            "received_at": now.isoformat(),
                            "event_time_basis": "exchange book update timestamp",
                            "receipt_basis": "adapter sample time; original socket receipt timestamp unavailable",
                            "session_id": self.kraken_sessions.get("spot"),
                            "depth_levels": min(len(bids), len(asks)),
                        }
                    )
                    self.kraken_book_at = book.at
        for observation in observations:
            label = observation["source"]
            old = self.feeds.get(label, {})
            self.feeds[label] = {
                **old,
                "connected": True,
                "price": observation["price"],
                "observed_at": observation["received_at"],
                "event_time": observation["event_at"],
                "source": label + " public WebSocket (proxy, NOT settlement index)",
                "receipt_basis": observation["receipt_basis"],
                "session_id": observation["session_id"],
            }
            if observation["kind"] == "quote":
                self.feeds[label].update(
                    bid=observation["bid"],
                    ask=observation["ask"],
                    quote_received_at=observation["received_at"],
                )
            self._observe(observation)

    def snapshot(self, now=None):
        now = now or utcnow()
        with self.condition:
            self._sample_kraken(now)
            feeds = {}
            for label in (
                "Coinbase BTC/USD stream",
                "Kraken BTC/USD stream",
                "Kraken futures stream",
            ):
                data = deepcopy(self.feeds.get(label, {}))
                age = (
                    (now - timestamp(data["observed_at"])).total_seconds()
                    if data.get("observed_at")
                    else None
                )
                event_age = (
                    (now - timestamp(data["event_time"])).total_seconds()
                    if data.get("event_time")
                    else None
                )
                fresh = bool(
                    data.get("connected")
                    and age is not None
                    and 0 <= age <= MAX_AGE
                    and (event_age is None or 0 <= event_age <= MAX_AGE)
                )
                delay = (
                    (timestamp(data["observed_at"]) - timestamp(data["event_time"])).total_seconds()
                    * 1000
                    if data.get("observed_at") and data.get("event_time")
                    else None
                )
                quote_age = (
                    (now - timestamp(data["quote_received_at"])).total_seconds()
                    if data.get("quote_received_at")
                    else None
                )
                if quote_age is None or not 0 <= quote_age <= MAX_AGE:
                    data.update(bid=None, ask=None, bid_size=None, ask_size=None)
                feeds[label] = {
                    "status": "LIVE"
                    if fresh
                    else "STALE"
                    if data.get("observed_at")
                    else "CONNECTING",
                    "data": data,
                    "age_seconds": age,
                    "exchange_event_age_seconds": event_age,
                    "event_to_observed_ms": delay,
                    "latency_note": "Event-to-observed difference includes clock offset and publication delay; not measured one-way network latency",
                }
            candidates = [
                feeds[name] for name in ("Coinbase BTC/USD stream", "Kraken BTC/USD stream")
            ]
            chosen = next((f["data"] for f in candidates if f["status"] == "LIVE"), None)
            reference = (
                {
                    k: chosen.get(k)
                    for k in (
                        "price",
                        "source",
                        "observed_at",
                        "event_time",
                        "receipt_basis",
                        "session_id",
                    )
                }
                if chosen
                else None
            )
            if reference:
                reference.update(official=False, transport="public WebSocket")
            return {
                "sequence": self.sequence,
                "reference": reference,
                "feeds": feeds,
                "diagnostics": {
                    **self.metrics,
                    "pending_observations": len(self.pending),
                    "capacity": self.pending.maxlen,
                    "coalescing": "Latest state publication; oldest pending observations discarded on overflow, with explicit gap count",
                    "kalshi_websocket": "Not anonymous: requires separately verified read-only market-data connection; public REST fallback remains available",
                },
            }

    def drain_observations(self):
        with self.condition:
            items = list(self.pending)
            self.pending.clear()
            if items and self.dropped_since_drain:
                items[0] = {
                    **items[0],
                    "gap_detected": True,
                    "missing_updates_count": self.dropped_since_drain,
                }
                self.dropped_since_drain = 0
            return items

    def wait_for_update(self, sequence, timeout=0.5):
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence > sequence or self.stop.is_set(), min(0.5, max(0, timeout))
            )
            return self.sequence

    def run(self):
        connector = self.connector
        if connector is None:
            try:
                from websockets.sync.client import connect

                connector = connect
            except ImportError:
                self.disconnected("WEBSOCKETS_DEPENDENCY_UNAVAILABLE")
                return
        attempt = 0
        while not self.stop.is_set():
            try:
                with self.condition:
                    self.metrics["connection_attempts"] += 1
                    self.metrics["reconnects"] = max(0, self.metrics["connection_attempts"] - 1)
                self.begin_session("coinbase")
                with connector(
                    COINBASE_WS, open_timeout=5, close_timeout=1, max_size=2**20, max_queue=16
                ) as ws:
                    self.socket = ws
                    # Three public subscriptions are below Coinbase's documented
                    # eight unauthenticated messages/second connection limit.
                    for channel in ("heartbeats", "ticker", "market_trades"):
                        ws.send(
                            json.dumps(
                                {
                                    "type": "subscribe",
                                    "product_ids": ["BTC-USD"],
                                    "channel": channel,
                                }
                            )
                        )
                    last_message = monotonic()
                    while not self.stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            if monotonic() - last_message > 20:
                                ping_started = perf_counter()
                                if not ws.ping().wait(timeout=5):
                                    raise TimeoutError("Stream heartbeat timeout")
                                self.metrics["last_transport_ping_ms"] = round(
                                    (perf_counter() - ping_started) * 1000, 3
                                )
                                last_message = monotonic()
                            continue
                        self.process_coinbase(raw, utcnow(), monotonic())
                        last_message = monotonic()
                        attempt = 0
            except Exception as exc:
                self.disconnected(exc)
                attempt += 1
                self.stop.wait(min(30, 2 ** min(attempt - 1, 5)) * random.uniform(0.8, 1.2))
            finally:
                self.socket = None

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
            ws = self.socket
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

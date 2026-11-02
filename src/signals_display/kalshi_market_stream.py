"""Isolated, authenticated market-data subscription; never an execution client.

The only application message is a fixed orderbook_delta subscription. The
read-only key must be verified before the authenticated GET upgrade and while
connected. Credentials and transport exception bodies never enter snapshots.

Schema references: Kalshi WebSocket orderbook updates and the cached official
order-direction guide. use_yes_price=True explicitly fixes both sides to YES-leg
prices, avoiding a future default change. Unsupported schemas fail closed.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import random
import re
import threading
from time import monotonic, perf_counter

from .model import timestamp

URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
MAX_TICKERS = 24
MAX_LEVELS = 10000
MAX_AGE_SECONDS = 5
TICKER = re.compile(r"KXBTC15M-[A-Z0-9-]{1,100}\Z")


def utcnow():
    return datetime.now(timezone.utc)


def decimal(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Unsupported numeric format")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid number") from None
    if not number.is_finite() or not low <= number <= high:
        raise ValueError("Number outside supported bounds")
    return number


def _levels(raw, *, cents=False):
    if not isinstance(raw, list) or len(raw) > MAX_LEVELS:
        raise ValueError("Unsupported book levels")
    result = {}
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("Unsupported book level")
        price = decimal(row[0], 0, 100 if cents else 1) / (100 if cents else 1)
        quantity = decimal(row[1], 0, Decimal("1e12"))
        if price in result:
            raise ValueError("Duplicate price level")
        if quantity:
            result[price] = quantity
    return result


def _snapshot_side(payload, side):
    variants = []
    for field, cents in ((side + "_dollars_fp", False), (side + "_dollars", False), (side, True)):
        if field in payload:
            variants.append(_levels(payload[field], cents=cents))
    if not variants or any(v != variants[0] for v in variants):
        raise ValueError("Missing or contradictory book schema")
    return variants[0]


def _event_time(payload, now):
    """Only explicit supported units; an opaque numeric ts is never guessed."""
    raw = payload.get("timestamp", payload.get("ts"))
    if isinstance(raw, str) and ("T" in raw):
        event = timestamp(raw)
    elif payload.get("ts_ms") is not None:
        value = decimal(payload["ts_ms"], 0, Decimal("1e15"))
        event = datetime.fromtimestamp(float(value) / 1000, timezone.utc)
    else:
        return None
    if not 0 <= (now - event).total_seconds() <= MAX_AGE_SECONDS:
        raise ValueError("Stale or future exchange timestamp")
    return event


class KalshiMarketStream:
    """Fixed host/channel with bounded book state and exponential reconnects."""

    def __init__(self, client, *, connector=None, autostart=True):
        self.client = client
        self.connector = connector
        self.condition = threading.Condition(threading.RLock())
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.tickers = ()
        self.version = 0
        self.sequence = 0
        self.books = {}
        self.sid_sequences = {}
        self.seen = OrderedDict()
        self.status = "WAITING FOR VERIFIED READ-ONLY CONNECTION"
        self.error = None
        self.connected_at = None
        self.last_message_at = None
        self.last_permission_check = None
        self.reconnects = 0
        self.sequence_gaps = 0
        self.duplicates = 0
        self.messages = 0
        self.last_processing_ms = None
        self.last_network_rtt_ms = None
        self._ws = None
        self.thread = None
        if autostart:
            self.thread = threading.Thread(
                target=self.run, name="read-only-kalshi-book", daemon=True
            )
            self.thread.start()

    def set_tickers(self, tickers):
        items = list(tickers)
        if any(not isinstance(t, str) or not TICKER.fullmatch(t) for t in items):
            raise ValueError("Only up to 24 KXBTC15M market subscriptions are supported")
        items = sorted(set(items))
        if len(items) > MAX_TICKERS:
            raise ValueError("Only up to 24 KXBTC15M market subscriptions are supported")
        with self.condition:
            if tuple(items) == self.tickers:
                return
            self.tickers = tuple(items)
            self.version += 1
            # Reconnect obtains fresh snapshots, including still-held contracts.
            self._invalidate("SUBSCRIPTION CHANGED — WAITING FOR SNAPSHOTS")
            self.wake.set()

    def _invalidate(self, status):
        self.books = {}
        self.sid_sequences = {}
        self.seen = OrderedDict()
        self.status = status
        self.sequence += 1
        self.condition.notify_all()

    def subscription(self):
        """No caller-provided commands, channels, prices or account identifiers."""
        with self.condition:
            if not self.tickers:
                raise ValueError("No supported market selected")
            return {
                "id": 1,
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": list(self.tickers),
                    "use_yes_price": True,
                },
            }

    def _verify(self):
        permissions = self.client.permissions()
        if not permissions.get("read_only_verified") or permissions.get("account_access_blocker"):
            raise PermissionError("Verified read-only permissions required")
        self.last_permission_check = monotonic()

    def process(self, data, *, now=None):
        """Apply a decoded frame. Used by the worker and offline regression tests."""
        now = now or utcnow()
        started = perf_counter()
        with self.condition:
            try:
                changed = self._process(data, now)
            except Exception:
                self._invalidate("UNAVAILABLE — BOOK INVALID; RECONNECT REQUIRED")
                raise
            self.last_processing_ms = (perf_counter() - started) * 1000
            return changed

    def _process(self, data, now):
        if not isinstance(data, dict):
            raise ValueError("Unsupported WebSocket message")
        kind = data.get("type")
        if kind == "error":
            raise ValueError("Market-data subscription rejected")
        if kind not in {"orderbook_snapshot", "orderbook_delta"}:
            # Acknowledgements are not data and never refresh a price timestamp.
            return False
        msg = data.get("msg")
        if not isinstance(msg, dict) or msg.get("market_ticker") not in self.tickers:
            raise ValueError("Unrequested market data")
        sid, seq = data.get("sid"), data.get("seq")
        if type(sid) is not int or type(seq) is not int or sid < 1 or seq < 1:
            raise ValueError("Missing subscription sequence")
        fingerprint = hashlib.sha256(
            json.dumps(data, sort_keys=True, default=str).encode()
        ).digest()
        key = (sid, seq)
        if key in self.seen:
            if self.seen[key] != fingerprint:
                raise ValueError("Contradictory duplicate sequence")
            self.duplicates += 1
            return False
        prior = self.sid_sequences.get(sid)
        if prior is not None and seq != prior + 1:
            self.sequence_gaps += 1
            raise ValueError("Orderbook sequence gap")
        ticker = msg["market_ticker"]
        event = _event_time(msg, now)
        previous_event = self.books.get(ticker, {}).get("event_at")
        if event is not None and previous_event is not None and event < previous_event:
            raise ValueError("Out-of-order exchange timestamp")
        if kind == "orderbook_snapshot":
            book = {
                "yes": _snapshot_side(msg, "yes"),
                "no": _snapshot_side(msg, "no"),
                "sid": sid,
            }
        else:
            if ticker not in self.books or self.books[ticker]["sid"] != sid:
                raise ValueError("Delta before matching snapshot")
            previous = self.books[ticker]
            book = {**previous, "yes": dict(previous["yes"]), "no": dict(previous["no"])}
            side = msg.get("side")
            if side not in {"yes", "no"}:
                raise ValueError("Unsupported book side")
            prices = []
            for field, cents in (("price_dollars", False), ("price", True)):
                if field in msg:
                    prices.append(
                        decimal(msg[field], 0, 100 if cents else 1) / (100 if cents else 1)
                    )
            deltas = [
                decimal(msg[field], Decimal("-1e12"), Decimal("1e12"))
                for field in ("delta_fp", "delta")
                if field in msg
            ]
            if (
                not prices
                or not deltas
                or any(p != prices[0] for p in prices)
                or any(d != deltas[0] for d in deltas)
            ):
                raise ValueError("Missing or contradictory delta schema")
            price, delta = prices[0], deltas[0]
            quantity = book[side].get(price, Decimal(0)) + delta
            if quantity < 0 or quantity > Decimal("1e12"):
                raise ValueError("Invalid resulting book quantity")
            if quantity:
                book[side][price] = quantity
            else:
                book[side].pop(price, None)
            if len(book[side]) > MAX_LEVELS:
                raise ValueError("Book exceeded bounded depth")
        # With explicit YES pricing, YES side is bids and NO side is asks.
        if book["yes"] and book["no"] and max(book["yes"]) >= min(book["no"]):
            raise ValueError("Crossed or locked book")
        book.update(received_at=now, event_at=event, sequence=seq)
        self.books[ticker] = book
        self.sid_sequences[sid] = seq
        self.seen[key] = fingerprint
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        self.last_message_at = now
        self.messages += 1
        self.status = "CONNECTED"
        self.error = None
        self.sequence += 1
        self.condition.notify_all()
        return True

    def snapshot(self, ticker, now):
        with self.condition:
            book = self.books.get(ticker)
            age = (now - book["received_at"]).total_seconds() if book else None
            complete = bool(book and book["yes"] and book["no"])
            fresh = complete and age is not None and 0 <= age <= MAX_AGE_SECONDS
            quote = None
            if fresh:
                yes_bid, yes_ask = max(book["yes"]), min(book["no"])
                quote = {
                    "up": {
                        "bid": float(yes_bid),
                        "ask": float(yes_ask),
                        "bid_size": float(book["yes"][yes_bid]),
                        "ask_size": float(book["no"][yes_ask]),
                    },
                    "down": {
                        "bid": float(1 - yes_ask),
                        "ask": float(1 - yes_bid),
                        "bid_size": float(book["no"][yes_ask]),
                        "ask_size": float(book["yes"][yes_bid]),
                    },
                }
            event = book.get("event_at") if book else None
            return {
                "status": "LIVE" if fresh else "STALE" if book else self.status,
                "ticker": ticker,
                "quotes": quote,
                "observed_at": book["received_at"].isoformat() if book else None,
                "event_time": event.isoformat() if event else None,
                "age_seconds": round(age, 6) if age is not None else None,
                "feed_delay_ms": (book["received_at"] - event).total_seconds() * 1000
                if event
                else None,
                "sequence": book["sequence"] if book else None,
                "levels": {"yes": len(book["yes"]), "no": len(book["no"])} if book else None,
                "source": "Kalshi authenticated read-only orderbook WebSocket; explicit unified YES prices",
                "diagnostics": {
                    "connection": self.status,
                    "reason": self.error,
                    "messages": self.messages,
                    "reconnects": self.reconnects,
                    "sequence_gaps": self.sequence_gaps,
                    "duplicates": self.duplicates,
                    "processing_ms": self.last_processing_ms,
                    "network_ping_rtt_ms": self.last_network_rtt_ms,
                    "event_time_note": "Exchange timestamp when explicit ISO or ts_ms; opaque numeric ts is unavailable, not guessed",
                    "permission_gate": "Broker read-only metadata verified before handshake and periodically while connected",
                    "schema_verification": "Offline fixture compatibility; live provider connection must be verified in the Codespace",
                },
            }

    def wait_for_update(self, sequence, timeout=0.5):
        with self.condition:
            self.condition.wait_for(lambda: self.sequence > sequence or self.stop.is_set(), timeout)
            return self.sequence

    def run(self):
        connector = self.connector
        if connector is None:
            try:
                from websockets.sync.client import connect

                connector = connect
            except ImportError:
                with self.condition:
                    self.error = "UNAVAILABLE: WebSocket dependency missing"
                    self._invalidate("UNAVAILABLE")
                return
        delay = 1.0
        attempts = 0
        while not self.stop.is_set():
            with self.condition:
                version, tickers = self.version, self.tickers
            if not tickers:
                self.wake.wait(0.5)
                self.wake.clear()
                continue
            try:
                self._verify()
                headers = self.client.websocket_headers()
                with connector(
                    URL,
                    additional_headers=headers,
                    open_timeout=10,
                    close_timeout=2,
                    max_size=2**20,
                    max_queue=16,
                    ping_interval=None,
                ) as ws:
                    self._ws = ws
                    attempts += 1
                    with self.condition:
                        self.reconnects += int(attempts > 1)
                        self.connected_at = utcnow()
                        self._invalidate("CONNECTED — WAITING FOR SNAPSHOTS")
                    ws.send(json.dumps(self.subscription()))
                    last_ping = monotonic()
                    while not self.stop.is_set():
                        with self.condition:
                            if self.version != version:
                                break
                        if monotonic() - self.last_permission_check >= 10:
                            self._verify()
                        if monotonic() - last_ping >= 10:
                            timer = perf_counter()
                            if not ws.ping().wait(timeout=5):
                                raise TimeoutError("Market WebSocket heartbeat unavailable")
                            self.last_network_rtt_ms = (perf_counter() - timer) * 1000
                            last_ping = monotonic()
                        try:
                            message = ws.recv(timeout=0.5)
                        except TimeoutError:
                            continue
                        changed = self.process(json.loads(message, parse_float=Decimal))
                        if changed:
                            delay = 1.0
                self._ws = None
            except Exception as exc:
                self._ws = None
                with self.condition:
                    self.error = (
                        "Market-data connection unavailable ("
                        + type(exc).__name__
                        + "); private transport details redacted"
                    )
                    self._invalidate("UNAVAILABLE — RECONNECTING")
                self.wake.wait(delay + random.uniform(0, min(delay / 4, 1)))
                self.wake.clear()
                delay = min(30.0, delay * 2)

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.condition:
            self._invalidate("CLOSED")
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

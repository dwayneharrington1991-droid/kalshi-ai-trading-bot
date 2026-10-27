"""Public Kraken order flow. Experimental diagnostics, never probability overrides."""
from collections import deque
from decimal import Decimal
from datetime import datetime, timezone
import json
import threading
import zlib

from .model import timestamp, number
from .edge_reading import flow_metrics


def utcnow():
    return datetime.now(timezone.utc)


class Flow:
    def __init__(self, unit):
        self.unit = unit
        self.reset()

    def reset(self):
        self.started = None
        self.trades = deque(maxlen=100000)
        self.ids = set()
        self.cumulative = 0.0
        self.last = None
        self.peak = None
        self.divergence = None
        self.minute = None
        self.bars = deque(maxlen=20)

    def trade(self, identifier, at, price, quantity, side, now):
        if identifier in self.ids:
            return
        if not 0 <= (now - at).total_seconds() <= 5 or (self.last and at < self.last):
            raise ValueError("Stale/out-of-order trade; reset CVD session")
        price = number(float(price), 0.000001, 1e12)
        quantity = number(float(quantity), 0.000001, 1e15)
        if side not in ("buy", "sell"):
            raise ValueError("Unknown aggressor side")
        if self.started is None:
            self.started = at
        # Bound duplicate history; session CVD remains explicitly since reconnect.
        if len(self.trades) == self.trades.maxlen:
            self.ids.discard(self.trades[0][0])
        self.ids.add(identifier)
        self.cumulative += quantity if side == "buy" else -quantity
        self.trades.append((identifier, at, price, quantity, side, self.cumulative))
        self.last = at
        minute = int(at.timestamp()) // 60
        if self.minute and minute != self.minute["minute"]:
            if minute != self.minute["minute"] + 1:
                self.bars.clear()
                self.peak = None
                self.divergence = None
            self.bars.append(self.minute)
            if len(self.bars) >= 3:
                a, b, c = list(self.bars)[-3:]
                if b["high"] > a["high"] and b["high"] > c["high"]:
                    # Confirm a peak only after the following minute has closed.
                    if (
                        self.peak
                        and b["high"] > self.peak["high"]
                        and b["cvd_at_high"] < self.peak["cvd_at_high"]
                    ):
                        self.divergence = at
                    self.peak = b
            self.minute = None
        if self.minute is None:
            self.minute = dict(minute=minute, high=price, cvd_at_high=self.cumulative)
        elif price > self.minute["high"]:
            self.minute.update(high=price, cvd_at_high=self.cumulative)

    def snapshot(self, now):
        fresh = self.last is not None and 0 <= (now - self.last).total_seconds() <= 5
        recent = [t for t in self.trades if 0 <= (now - t[1]).total_seconds() <= 60]
        total = sum(t[3] for t in recent)
        delta = sum(t[3] if t[4] == "buy" else -t[3] for t in recent)
        covered = (
            bool(recent)
            and (now - recent[0][1]).total_seconds() >= 55
            and self.started
            and (now - self.started).total_seconds() >= 60
        )
        ready = bool(fresh and covered and len(recent) >= 20 and total > 0)
        fraction = delta / total if ready else None
        flat = ready and abs(recent[-1][2] / recent[0][2] - 1) <= 0.0001
        return dict(
            status="LIVE" if ready else "WARMING UP" if fresh else "STALE",
            observed_at=self.last.isoformat() if self.last else None,
            cvd_since_reconnect=self.cumulative if fresh else None,
            unit=self.unit,
            delta_60s=delta if ready else None,
            delta_fraction_60s=fraction,
            observed_trades_60s=len(recent),
            hidden_bullish_accumulation_candidate=False,
            passive_sell_absorption_candidate=bool(flat and fraction >= 0.3),
            absorption_note="Flat price with net aggressive buys is an ambiguous absorption candidate, not established bullish accumulation or institutional selling. The earlier bullish label is suppressed.",
            delta_efficiency_3m=flow_metrics(self.trades, self.started, now),
            bearish_divergence_candidate=bool(
                ready and self.divergence and 0 <= (now - self.divergence).total_seconds() <= 180
            ),
            assumptions="Experimental: flat means <=1bp observed 60s price change; rising CVD means net buy fraction >=30%, >=20 trades and >=55s window coverage after 60s warm-up. Divergence uses confirmed closed-minute price peaks and CVD at each peak. Neither implies high probability or an entry.",
            probability_adjustment=0.0,
            validation="experimental; edge unverified",
        )


class Book:
    def __init__(self):
        self.bids = {}
        self.asks = {}
        self.at = None
        self.ready = False

    @staticmethod
    def digits(value):
        return format(value, "f").replace(".", "").lstrip("0")

    def update(self, data, snapshot, now):
        try:
            self._update(data, snapshot, now)
        except Exception:
            self.ready = False
            raise

    def _update(self, data, snapshot, now):
        if snapshot:
            self.bids.clear()
            self.asks.clear()
            self.ready = False
        elif not self.ready:
            raise ValueError("Missing book snapshot")
        at = timestamp(data["timestamp"]) if data.get("timestamp") else now
        if not 0 <= (now - at).total_seconds() <= 5 or (self.at and not snapshot and at < self.at):
            raise ValueError("Stale/out-of-order book")
        for name, book, reverse in (("bids", self.bids, True), ("asks", self.asks, False)):
            for level in data.get(name, []):
                price = Decimal(str(level["price"]))
                qty = Decimal(str(level["qty"]))
                if not price.is_finite() or not qty.is_finite() or price <= 0 or qty < 0:
                    raise ValueError("Invalid book level")
                if qty == 0:
                    book.pop(price, None)
                else:
                    book[price] = qty
            # Subscribed depth is 25; Kraken checksum covers best ten.
            for p in sorted(book, reverse=reverse)[25:]:
                book.pop(p)
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            raise ValueError("Empty/crossed book")
        raw = "".join(
            self.digits(p) + self.digits(book[p])
            for book, reverse in ((self.asks, False), (self.bids, True))
            for p in sorted(book, reverse=reverse)[:10]
        )
        if zlib.crc32(raw.encode()) != int(data["checksum"]):
            self.ready = False
            raise ValueError("Book checksum failed; resubscribe required")
        self.ready = True
        self.at = at

    def snapshot(self, now):
        valid = (
            self.ready
            and self.at
            and 0 <= (now - self.at).total_seconds() <= 5
            and min(len(self.bids), len(self.asks)) >= 20
        )
        buy = sum(self.bids[p] for p in sorted(self.bids, reverse=True)[:20]) if valid else None
        sell = sum(self.asks[p] for p in sorted(self.asks)[:20]) if valid else None
        ratio = float(buy / sell) if valid and sell else None
        return dict(
            status="LIVE" if valid else "UNAVAILABLE",
            observed_at=self.at.isoformat() if self.at else None,
            top_levels=20,
            quantity_unit="BTC",
            bid_ask_ratio=ratio,
            dominant_side="UP"
            if ratio and ratio > 2.5
            else "DOWN"
            if ratio and ratio < 1 / 2.5
            else None,
            probability_adjustment=0.0,
            note="Checksum-verified public spot book; resting limits can be canceled/spoofed and are not executions. Fixed +8 percentage-point adjustment withheld pending chronological validation.",
        )


class PublicOrderFlow:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.spot = Flow("BTC")
        self.perp = Flow("PF_XBTUSD raw trade quantity; not pooled with spot BTC")
        self.book = Book()
        self.errors = {"spot": "CONNECTING", "perpetual": "CONNECTING"}
        self.threads = []
        for venue in ("spot", "perpetual"):
            t = threading.Thread(
                target=self.run, args=(venue,), daemon=True, name="public-flow-" + venue
            )
            t.start()
            self.threads.append(t)

    def run(self, venue):
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.errors[venue] = "UNAVAILABLE: install websockets==16.1.1"
            return
        delay = 1
        while not self.stop.is_set():
            try:
                url = (
                    "wss://ws.kraken.com/v2"
                    if venue == "spot"
                    else "wss://futures.kraken.com/ws/v1"
                )
                with connect(url, open_timeout=10, close_timeout=2, max_size=2**20) as ws:
                    with self.lock:
                        flow = self.spot if venue == "spot" else self.perp
                        flow.reset()
                        if venue == "spot":
                            self.book = Book()
                    if venue == "spot":
                        for channel in ("trade", "book"):
                            params = {"channel": channel, "symbol": ["BTC/USD"]}
                            params.update(
                                {"depth": 25, "snapshot": True}
                                if channel == "book"
                                else {"snapshot": False}
                            )
                            ws.send(json.dumps({"method": "subscribe", "params": params}))
                    else:
                        ws.send(
                            json.dumps(
                                {
                                    "event": "subscribe",
                                    "feed": "trade",
                                    "product_ids": ["PF_XBTUSD"],
                                }
                            )
                        )
                    sequence = None
                    delay = 1
                    while not self.stop.is_set():
                        try:
                            message = ws.recv(timeout=15)
                        except TimeoutError:
                            if not ws.ping().wait(timeout=5):
                                raise TimeoutError("Public WebSocket heartbeat failed")
                            continue
                        data = json.loads(message, parse_float=Decimal)
                        now = utcnow()
                        if data.get("success") is False or data.get("event") == "error":
                            raise ValueError("Subscription rejected")
                        with self.lock:
                            if (
                                venue == "spot"
                                and data.get("channel") == "trade"
                                and data.get("type") == "update"
                            ):
                                for t in data["data"]:
                                    if t["symbol"] != "BTC/USD":
                                        raise ValueError("Wrong trade symbol")
                                    flow.trade(
                                        str(t["trade_id"]),
                                        timestamp(t["timestamp"]),
                                        t["price"],
                                        t["qty"],
                                        t["side"],
                                        now,
                                    )
                            elif venue == "spot" and data.get("channel") == "book":
                                for b in data["data"]:
                                    if b["symbol"] != "BTC/USD":
                                        raise ValueError("Wrong book symbol")
                                    self.book.update(b, data["type"] == "snapshot", now)
                            elif (
                                venue == "perpetual"
                                and data.get("feed") == "trade"
                                and not data.get("event")
                            ):
                                if data["product_id"] != "PF_XBTUSD":
                                    raise ValueError("Wrong perpetual symbol")
                                seq = int(data["seq"])
                                if sequence is not None and seq != sequence + 1:
                                    raise ValueError("Perpetual trade sequence gap")
                                sequence = seq
                                at = datetime.fromtimestamp(int(data["time"]) / 1000, timezone.utc)
                                flow.trade(
                                    str(data["uid"]),
                                    at,
                                    data["price"],
                                    data["qty"],
                                    data["side"],
                                    now,
                                )
                            self.errors[venue] = None
            except Exception as exc:
                with self.lock:
                    (self.spot if venue == "spot" else self.perp).reset()
                    if venue == "spot":
                        self.book = Book()
                    self.errors[venue] = (
                        "UNAVAILABLE: " + type(exc).__name__ + "; reconnecting; CVD reset"
                    )
                self.stop.wait(delay)
                delay = min(30, delay * 2)

    def snapshot(self, now):
        with self.lock:
            return {
                "spot": self.spot.snapshot(now),
                "perpetual": self.perp.snapshot(now),
                "spot_book": self.book.snapshot(now),
                "connection_errors": dict(self.errors),
                "source": "Kraken public WebSockets only; spot and perpetual quantities are never pooled",
            }

    def close(self):
        self.stop.set()

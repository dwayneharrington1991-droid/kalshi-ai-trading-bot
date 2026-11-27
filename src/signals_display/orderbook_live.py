"""Live BTC spot order books and trade flow for the dashboard (read-only market data; research and display only).

Pure logic, stdlib only; the network loop is scripts/orderbook_service.py. Books are kept in memory per venue
(Coinbase BTC-USD level2, Kraken BTC/USD book depth 25) and never combined: each venue has its own prices,
sizes (BTC) and freshness. Nothing here predicts anything; "BUY/SELL/MIXED pressure" is a stated descriptive rule.

Integrity:
  * Coinbase: snapshot then updates; the message sequence_num must increase by exactly 1, else the book is marked
    RESYNC and rebuilt from a new snapshot.
  * Kraken v2: snapshot then updates, each carrying the exchange CRC32 checksum of the top 10 asks and bids; a
    mismatch marks the book RESYNC. The book is truncated to the subscribed depth after every update.
  * A book is LIVE only if its last exchange event is fresher than STALE_S; otherwise STALE (never shown as live).
Trade side: Kraken `side` is the taker (aggressor). Coinbase market_trades `side` is the MAKER side, so the
aggressor is the opposite (same convention as streaming.py).
"""
import time
import zlib
from collections import deque
from decimal import Decimal
from math import isfinite

STALE_S = 5.0
KRAKEN_DEPTH = 25
TOP = 20
WALL_FACTOR = 5.0          # a wall: size >= 5x the median of the top 20 levels on that side (largest 3 per side)
WALLS_PER_SIDE = 3
LARGE_TRADE_BTC = 1.0
WINDOWS = (1, 5, 15, 30, 60, 300)


class Book:
    """One venue's price-level book: price (Decimal) -> size (Decimal). Bids and asks kept sorted."""

    def __init__(self, venue, depth=None):
        self.venue, self.depth = venue, depth
        self.bids, self.asks = {}, {}
        self.status = "CONNECTING"
        self.last_event = None          # exchange event time (epoch seconds) of the last applied change
        self.last_receipt = None        # local receipt time (epoch seconds)
        self.resyncs = 0

    def reset(self, status="RESYNC"):
        self.bids.clear(); self.asks.clear(); self.status = status; self.resyncs += status == "RESYNC"

    def apply(self, side, price, size):
        book = self.bids if side == "bid" else self.asks
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size

    def truncate(self):
        if self.depth:
            for book, rev in ((self.bids, True), (self.asks, False)):
                for p in sorted(book, reverse=rev)[self.depth:]:
                    del book[p]

    def levels(self, n=TOP):
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:n]
        return bids, asks

    def freshness(self, now=None):
        now = now or time.time()
        if self.status in ("RESYNC", "CONNECTING", "DISCONNECTED"):
            return self.status
        if (self.last_event is None or self.last_receipt is None
                or not self.last_event <= self.last_receipt <= now
                or not 0 <= now - self.last_event <= STALE_S or not self.bids or not self.asks):
            return "STALE"
        best_bid, best_ask = max(self.bids), min(self.asks)
        return "LIVE" if best_bid < best_ask else "RESYNC"          # crossed book = corrupt


def kraken_checksum(book, price_dp=1, qty_dp=8):
    """CRC32 of the top 10 asks (ascending) then top 10 bids (descending): price and qty formatted to their
    precision, decimal point removed, leading zeros stripped (Kraken WebSocket v2 book checksum)."""
    def fmt(v, dp):
        s = f"{v:.{dp}f}".replace(".", "").lstrip("0")
        return s or "0"
    bids, asks = book.levels(10)
    s = "".join(fmt(p, price_dp) + fmt(q, qty_dp) for p, q in asks) + \
        "".join(fmt(p, price_dp) + fmt(q, qty_dp) for p, q in bids)
    return zlib.crc32(s.encode()) & 0xFFFFFFFF


class SequenceTracker:
    """Coinbase sequence_num: must increase by exactly one per message on the subscription."""

    def __init__(self):
        self.last = None

    def ok(self, seq):
        good = self.last is None or seq == self.last + 1
        self.last = seq
        return good


class TradeFlow:
    """Executed trades (aggressor side known) in a bounded time window; CVD since start."""

    def __init__(self, horizon_s=max(WINDOWS)):
        self.trades = deque()           # (time, side 'buy'|'sell', size float, price float)
        self.horizon = horizon_s
        self.cvd = 0.0
        self.seen = deque(maxlen=5000)  # recent trade ids, to drop duplicates
        self.seen_set = set()

    def add(self, ts, side, size, price, trade_id=None):
        if (side not in ("buy", "sell") or not all(isfinite(v) for v in (ts, size, price))
                or size <= 0 or price <= 0):
            return False
        if trade_id is not None:
            if trade_id in self.seen_set:
                return False
            if len(self.seen) == self.seen.maxlen:
                self.seen_set.discard(self.seen[0])
            self.seen.append(trade_id); self.seen_set.add(trade_id)
        self.trades.append((ts, side, size, price))
        self.cvd += size if side == "buy" else -size
        cutoff = ts - self.horizon
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()
        return True

    def windows(self, now):
        out = {}
        for w in WINDOWS:
            buy = sell = 0.0; n = large = 0
            for ts, side, size, _ in self.trades:
                if now - w < ts <= now:
                    n += 1
                    large += size >= LARGE_TRADE_BTC
                    if side == "buy":
                        buy += size
                    else:
                        sell += size
            tot = buy + sell
            out[f"{w}s"] = {"buy_btc": round(buy, 6), "sell_btc": round(sell, 6), "trades": n, "large_trades": large,
                            "imbalance": round((buy - sell) / tot, 4) if tot else None}
        return out


def depth_view(book, now=None, prev_walls=None, recent_trades=None):
    """Display/research snapshot of one book. prev_walls: {price_str: size} from the previous second, used to
    classify visible wall changes. Recent executions can corroborate a change at the same price and side."""
    now = now or time.time()
    bids, asks = book.levels(TOP)
    fresh = book.freshness(now)
    view = {"venue": book.venue, "status": fresh, "resyncs": book.resyncs,
            "last_event": book.last_event, "last_receipt": book.last_receipt,
            "age_s": round(now - book.last_event, 3) if book.last_event else None}
    if fresh != "LIVE":
        return view, {}
    def ladder(levels):
        cum, out = Decimal(0), []
        for p, q in levels:
            cum += q
            out.append([float(p), float(q), float(cum)])
        return out
    lb, la = ladder(bids), ladder(asks)
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / 2
    def band(levels, frac):
        lo, hi = mid * (1 - Decimal(str(frac))), mid * (1 + Decimal(str(frac)))
        return float(sum(q for p, q in levels if lo <= p <= hi))
    allb, alla = sorted(book.bids.items(), key=lambda kv: -kv[0]), sorted(book.asks.items(), key=lambda kv: kv[0])
    bands = {}
    for frac in (0.0005, 0.001, 0.0025, 0.005):
        b, a = band(allb, frac), band(alla, frac)
        bands[f"{frac*100:.2f}%"] = {"bid_btc": round(b, 6), "ask_btc": round(a, 6),
                                     "imbalance": round((b - a) / (b + a), 4) if b + a else None}
    walls, now_walls = [], {}
    for side, levels in (("bid", bids), ("ask", asks)):
        sizes = sorted(q for _, q in levels)
        med = sizes[len(sizes) // 2] if sizes else Decimal(0)
        big = sorted(((p, q) for p, q in levels if med > 0 and q >= med * Decimal(str(WALL_FACTOR))), key=lambda pq: -pq[1])
        for p, q in big[:WALLS_PER_SIDE]:
            walls.append({"side": side, "price": float(p), "size_btc": float(q), "x_median": round(float(q / med), 2),
                          "distance_pct": round(float((p - mid) / mid) * 100, 4)})
            now_walls[f"{side}:{p}"] = q
    events = []
    if prev_walls is not None:
        traded = {}
        for ts, side, size, price in (recent_trades.trades if recent_trades else []):
            if now - 1.5 < ts <= now:
                key = ("bid" if side == "sell" else "ask", Decimal(str(price)))
                traded[key] = traded.get(key, 0.0) + size
        for key, q in now_walls.items():
            old = prev_walls.get(key)
            if old is None:
                events.append({"wall": key, "event": "NEW", "size_btc": float(q)})
            elif q > old * Decimal("1.2"):
                events.append({"wall": key, "event": "GREW", "from": float(old), "to": float(q)})
            elif q < old * Decimal("0.8"):
                events.append({"wall": key, "event": "SHRANK", "from": float(old), "to": float(q)})
        for key, old in prev_walls.items():
            if key not in now_walls:
                side, price = key.split(":", 1)
                hit = traded.get((side, Decimal(price)), 0.0)
                kind = "MATCHING EXECUTIONS OBSERVED" if hit else "NO MATCHING EXECUTIONS OBSERVED"
                events.append({"wall": key, "event": "GONE", "size_btc": float(old),
                               "classification": kind, "matching_trade_btc": hit})
    view.update({"best_bid": float(best_bid), "best_ask": float(best_ask), "mid": float(mid),
                 "spread": float(best_ask - best_bid), "spread_bps": round(float((best_ask - best_bid) / mid) * 1e4, 3),
                 "bids": lb, "asks": la, "bands": bands, "walls": walls, "wall_events": events})
    return view, now_walls


def pressure(view, flow_windows):
    """Descriptive label only (not a prediction): BUY if both the +-0.25% book imbalance and the 60 s executed
    trade imbalance are >= +0.2, SELL if both <= -0.2, else MIXED; UNAVAILABLE if the book is not LIVE."""
    if view.get("status") != "LIVE":
        return {"label": "UNAVAILABLE", "rule": pressure.__doc__.split(": ", 1)[1]}
    b = (view.get("bands") or {}).get("0.25%", {}).get("imbalance")
    t = (flow_windows or {}).get("60s", {}).get("imbalance")
    if b is None or t is None:
        label = "MIXED"
    elif b >= 0.2 and t >= 0.2:
        label = "BUY"
    elif b <= -0.2 and t <= -0.2:
        label = "SELL"
    else:
        label = "MIXED"
    return {"label": label, "book_imbalance_0.25pct": b, "trade_imbalance_60s": t,
            "rule": "BUY if book imbalance (+-0.25%) and 60s trade imbalance are both >= +0.2; SELL if both <= -0.2; else MIXED. Descriptive, not a prediction."}


def latest_body(path=None, now=None):
    """Bytes for /api/orderbook: the collector's latest snapshot, or an honest UNAVAILABLE/STALE status."""
    import json
    from pathlib import Path
    path = Path(path) if path else Path.home() / "dual-ai-alerts" / "orderbook_latest.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return json.dumps({"status": "UNAVAILABLE", "reason": "order-book collector output not found"}).encode()
    try:
        generated = data["generated_at"]
        if type(generated) not in (float, int) or not isfinite(generated):
            raise ValueError("invalid generation time")
        age = (time.time() if now is None else now) - generated
        if age < 0:
            raise ValueError("future snapshot")
    except (TypeError, KeyError, ValueError):
        return json.dumps({"status": "UNAVAILABLE", "reason": "invalid order-book snapshot time"}).encode()
    if age > STALE_S:
        for v in (data.get("venues") or {}).values():
            v["status"] = "STALE"
        data["status"], data["reason"] = "STALE", f"collector not updating ({age:.0f}s old)"
    else:
        data["status"] = "OK"
    data["snapshot_age_s"] = round(age, 2)
    return json.dumps(data).encode()

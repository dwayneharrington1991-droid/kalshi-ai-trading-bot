"""Live BTC order books + trade flow collector (read-only public market data; signals-only bot, never trades).

Runs as its own low-priority service (deploy/oracle/kalshi-orderbook.service, CPUQuota/MemoryMax bounded), so the
8768 server is not loaded. Keeps Coinbase BTC-USD level2 and Kraken BTC/USD depth-25 books in memory
(src/signals_display/orderbook_live.py), plus executed trades, and every second:
  * writes ~/dual-ai-alerts/orderbook_latest.json (the dashboard reads it through /api/orderbook);
  * appends one compact row per venue to ~/dual-ai-alerts/orderbook_research.sqlite3 (its own file, never the
    journal), keeping RETENTION_DAYS of history.
Reconnects with capped exponential backoff. A checksum mismatch, sequence gap or crossed book marks the venue
RESYNC and reconnects; nothing stale is ever labelled LIVE.
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.signals_display import orderbook_live as ob  # noqa: E402

import websockets  # noqa: E402

BASE = Path.home() / "dual-ai-alerts"
OUT = BASE / "orderbook_latest.json"
DB = BASE / "orderbook_research.sqlite3"
RETENTION_DAYS = 14
KRAKEN = "wss://ws.kraken.com/v2"
COINBASE = "wss://advanced-trade-ws.coinbase.com"


def ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")[:32]).timestamp()
    except (TypeError, ValueError):
        return None


class State:
    def __init__(self):
        self.books = {"coinbase": ob.Book("Coinbase BTC-USD"), "kraken": ob.Book("Kraken BTC/USD", depth=ob.KRAKEN_DEPTH)}
        self.flows = {"coinbase": ob.TradeFlow(), "kraken": ob.TradeFlow()}
        self.prev_walls = {"coinbase": None, "kraken": None}
        self.messages = {"coinbase": 0, "kraken": 0}
        self.errors = {"coinbase": None, "kraken": None}


async def kraken(st):
    book, flow, delay = st.books["kraken"], st.flows["kraken"], 1
    while True:
        try:
            async with websockets.connect(KRAKEN, ping_interval=20, max_size=2 ** 22) as ws:
                book.reset("CONNECTING")
                for ch in ({"channel": "book", "symbol": ["BTC/USD"], "depth": ob.KRAKEN_DEPTH},
                           {"channel": "trade", "symbol": ["BTC/USD"]}):
                    await ws.send(json.dumps({"method": "subscribe", "params": ch}))
                async for raw in ws:
                    m = json.loads(raw, parse_float=Decimal)
                    st.messages["kraken"] += 1
                    ch, typ = m.get("channel"), m.get("type")
                    if ch == "book":
                        for d in m.get("data") or []:
                            if typ == "snapshot":
                                book.reset("CONNECTING")
                            for side, key in (("bid", "bids"), ("ask", "asks")):
                                for lv in d.get(key) or []:
                                    book.apply(side, Decimal(lv["price"]), Decimal(lv["qty"]))
                            book.truncate()
                            if ob.kraken_checksum(book) != int(d.get("checksum", -1)):
                                book.reset("RESYNC"); st.errors["kraken"] = "checksum mismatch"
                                raise ConnectionError("kraken checksum mismatch")
                            book.status = "OK"
                            book.last_event = ts(d.get("timestamp")) or time.time()
                            book.last_receipt = time.time()
                    elif ch == "trade" and typ == "update":
                        for t in m.get("data") or []:
                            flow.add(ts(t.get("timestamp")) or time.time(), "buy" if t.get("side") == "buy" else "sell",
                                     float(t["qty"]), float(t["price"]), trade_id=t.get("trade_id"))
            delay = 1
        except Exception as exc:                    # reconnect with capped backoff; never crash the service
            st.errors["kraken"] = f"{type(exc).__name__}: {exc}"[:160]
            book.status = "DISCONNECTED" if book.status != "RESYNC" else "RESYNC"
            await asyncio.sleep(delay); delay = min(delay * 2, 60)


async def coinbase(st):
    book, flow, delay = st.books["coinbase"], st.flows["coinbase"], 1
    while True:
        seq = ob.SequenceTracker()
        try:
            async with websockets.connect(COINBASE, ping_interval=20, max_size=2 ** 24) as ws:
                book.reset("CONNECTING")
                for ch in ("heartbeats", "level2", "market_trades"):
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": ch}))
                async for raw in ws:
                    m = json.loads(raw)
                    st.messages["coinbase"] += 1
                    if "sequence_num" in m and not seq.ok(int(m["sequence_num"])):
                        book.reset("RESYNC"); st.errors["coinbase"] = "sequence gap"
                        raise ConnectionError("coinbase sequence gap")
                    ch = m.get("channel")
                    if ch == "l2_data":
                        for ev in m.get("events") or []:
                            if ev.get("type") == "snapshot":
                                book.reset("CONNECTING")
                            for u in ev.get("updates") or []:
                                book.apply("bid" if u.get("side") == "bid" else "ask",
                                           Decimal(u["price_level"]), Decimal(u["new_quantity"]))
                        book.status = "OK"
                        book.last_event = ts(m.get("timestamp")) or time.time()
                        book.last_receipt = time.time()
                    elif ch == "market_trades":
                        for ev in m.get("events") or []:
                            if ev.get("type") != "update":
                                continue
                            for t in ev.get("trades") or []:
                                aggressor = "sell" if t.get("side") == "BUY" else "buy"     # side = maker side
                                flow.add(ts(t.get("time")) or time.time(), aggressor, float(t["size"]), float(t["price"]),
                                         trade_id=t.get("trade_id"))
            delay = 1
        except Exception as exc:
            st.errors["coinbase"] = f"{type(exc).__name__}: {exc}"[:160]
            book.status = "DISCONNECTED" if book.status != "RESYNC" else "RESYNC"
            await asyncio.sleep(delay); delay = min(delay * 2, 60)


def research_db():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS book_seconds(ts REAL, venue TEXT, status TEXT, mid REAL, spread REAL,
        bid_025 REAL, ask_025 REAL, imb_010 REAL, imb_025 REAL, buy_1s REAL, sell_1s REAL, cvd REAL,
        top_bid_wall REAL, top_bid_wall_px REAL, top_ask_wall REAL, top_ask_wall_px REAL, event_age REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS book_seconds_ts ON book_seconds(ts)")
    db.commit()
    return db


async def writer(st):
    db, last_prune = research_db(), 0.0
    while True:
        await asyncio.sleep(1.0 - (time.time() % 1.0))
        now = time.time()
        out, rows = {"generated_at": now, "venues": {}}, []
        for key in ("coinbase", "kraken"):
            view, walls = ob.depth_view(st.books[key], now, st.prev_walls[key], st.flows[key])
            st.prev_walls[key] = walls if view.get("status") == "LIVE" else None
            win = st.flows[key].windows(now)
            view.update(trades=win, cvd_btc=round(st.flows[key].cvd, 6), pressure=ob.pressure(view, win),
                        messages=st.messages[key], last_error=st.errors[key])
            out["venues"][key] = view
            if view.get("mid"):
                bw = max((w for w in view["walls"] if w["side"] == "bid"), key=lambda w: w["size_btc"], default=None)
                aw = max((w for w in view["walls"] if w["side"] == "ask"), key=lambda w: w["size_btc"], default=None)
                b = view["bands"]
                rows.append((now, key, view["status"], view["mid"], view["spread"], b["0.25%"]["bid_btc"], b["0.25%"]["ask_btc"],
                             b["0.10%"]["imbalance"], b["0.25%"]["imbalance"], win["1s"]["buy_btc"], win["1s"]["sell_btc"],
                             view["cvd_btc"], bw and bw["size_btc"], bw and bw["price"], aw and aw["size_btc"], aw and aw["price"],
                             view["age_s"]))
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, default=float))
        os.replace(tmp, OUT)
        if rows:
            db.executemany("INSERT INTO book_seconds VALUES (" + ",".join("?" * 17) + ")", rows)
            db.commit()
        if now - last_prune > 3600:
            db.execute("DELETE FROM book_seconds WHERE ts < ?", (now - RETENTION_DAYS * 86400,))   # own research file only
            db.commit(); last_prune = now


async def main():
    st = State()
    await asyncio.gather(kraken(st), coinbase(st), writer(st))


if __name__ == "__main__":
    asyncio.run(main())

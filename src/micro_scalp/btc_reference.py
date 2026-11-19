"""Public Coinbase BTC-USD trade tape -> 1-second bars (GET-only, no auth).

Kalshi KXBTC15M contracts settle on CF Benchmarks' BRTI. Coinbase BTC-USD is a
constituent exchange and serves as a *proxy* reference: it is not the
settlement index, and any basis to BRTI is unknown.

Bar for second s = trades with s <= time < s+1: last price (highest trade_id),
total size, taker-buy size, trade count. A bar becomes usable only after the
second has closed plus a receipt delay (see ``events.btc_events``).

    python -m src.micro_scalp.btc_reference --out data/micro_scalp/btc/btc_1s.json --since 2026-11-17T00:00:00Z
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import time

import httpx

from . import install_guard
from .public_data import PublicGetClient

URL = "https://api.exchange.coinbase.com/products/BTC-USD/trades"


def _ts(v: str) -> float:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def fetch_bars(since: float, until: float | None = None, client: PublicGetClient | None = None, pause: float = 0.15, max_requests: int = 20000):
    client = client or PublicGetClient(timeout=30)
    bars: dict[int, list] = {}  # sec -> [last_id, last_price, size, buy_size, n]
    after, requests, oldest = None, 0, None
    while requests < max_requests:
        params = {"limit": 1000}
        if after:
            params["after"] = after
        for attempt in range(8):
            try:
                time.sleep(pause)
                rows = client.get_json(URL, params)
                break
            except (httpx.HTTPStatusError, httpx.TransportError):
                time.sleep(min(30, 2 ** attempt))
        else:
            raise RuntimeError("Coinbase trade tape unavailable")
        requests += 1
        if not rows:
            break
        for r in rows:
            t = _ts(r["time"])
            if until is not None and t >= until:
                continue
            if t < since:
                continue
            sec, tid, px, sz = int(t), int(r["trade_id"]), float(r["price"]), float(r["size"])
            b = bars.get(sec)
            if b is None:
                b = bars[sec] = [tid, px, 0.0, 0.0, 0]
            if tid > b[0]:
                b[0], b[1] = tid, px
            b[2] += sz
            # Coinbase "side" is the maker side: side == "sell" means a taker BUY.
            b[3] += sz if r["side"] == "sell" else 0.0
            b[4] += 1
        oldest = _ts(rows[-1]["time"])
        after = rows[-1]["trade_id"]
        if oldest < since:
            break
        if requests % 200 == 0:
            print(f"{requests} requests, back to {rows[-1]['time']}", flush=True)
    return [[s, b[1], round(b[2], 8), round(b[3], 8), b[4]] for s, b in sorted(bars.items())], requests, oldest


def main():
    install_guard()
    p = argparse.ArgumentParser(description="Cache public Coinbase BTC-USD 1-second bars")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--since", required=True)
    p.add_argument("--until", default=None)
    a = p.parse_args()
    bars, n, oldest = fetch_bars(_ts(a.since), _ts(a.until) if a.until else None)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": "Coinbase BTC-USD public trades (proxy, not BRTI)", "fields": ["sec", "last", "size", "taker_buy_size", "trades"], "bars": bars}))
    tmp.replace(a.out)
    print(f"wrote {len(bars)} one-second bars from {n} requests (oldest trade {oldest})")


if __name__ == "__main__":
    main()

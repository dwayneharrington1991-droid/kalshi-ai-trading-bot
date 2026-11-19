"""Fetch settled KXBTC15M history from Kalshi's public API (GET-only, cached).

Per settled contract we store:
- metadata (open/close time, strike, official result);
- 1-minute YES bid/ask/trade candlesticks;
- the public trade tape (each print: exchange time, YES price, size, taker side).

Kalshi does not publish historical order-book depth. The replay therefore
infers executable prices only from observable evidence (candle closes and
trade prints) and never fabricates depth; see ``events.py``/``execution.py``.

    python -m src.micro_scalp.historical --out data/micro_scalp/historical --markets 300
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

SERIES = "KXBTC15M"
TAPE_END_BUFFER = 25


def _ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class HistoricalFetcher:
    def __init__(self, out: Path, client: PublicGetClient | None = None, pause: float = 0.12):
        self.out = out
        self.client = client or PublicGetClient(timeout=30)
        self.pause = pause
        out.mkdir(parents=True, exist_ok=True)

    def _get(self, path, params=None):
        for attempt in range(8):
            try:
                time.sleep(self.pause)
                return self.client.kalshi(path, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise
            except httpx.TransportError:
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError(f"Repeated failures for {path}")

    def settled_markets(self, count: int, before: str | None = None):
        """Newest settled markets that CLOSED before ``before`` (keeps the
        historical set disjoint from the fresh forward-test recording)."""
        cutoff = _ts(before) if before else None
        markets, cursor = [], None
        while len(markets) < count:
            params = {"series_ticker": SERIES, "status": "settled", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params)
            batch = [
                m
                for m in data.get("markets", [])
                if m.get("result") in ("yes", "no") and (cutoff is None or _ts(m["close_time"]) <= cutoff)
            ]
            markets.extend(batch)
            cursor = data.get("cursor")
            if not cursor or not data.get("markets"):
                break
        return markets[:count]

    def trades(self, ticker, min_ts, max_ts, max_pages=120):
        """Full tape for [min_ts, max_ts], stored compactly as
        [exchange_time_iso, yes_price, count, taker_side]."""
        out, cursor = [], None
        for _ in range(max_pages):
            params = {"ticker": ticker, "limit": 1000, "min_ts": min_ts, "max_ts": max_ts}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets/trades", params)
            out.extend(
                [t["created_time"], t["yes_price_dollars"], t["count_fp"], t["taker_side"]]
                for t in data.get("trades", [])
            )
            cursor = data.get("cursor")
            if not cursor or not data.get("trades"):
                return out, False
        return out, True  # truncated

    def fetch_market(self, m):
        ticker = m["ticker"]
        path = self.out / f"{ticker}.json"
        if path.exists():
            return path
        start, end = _ts(m["open_time"]), _ts(m["close_time"])
        candles = self._get(
            f"/series/{SERIES}/markets/{ticker}/candlesticks",
            {"start_ts": start, "end_ts": end, "period_interval": 1},
        ).get("candlesticks", [])
        # The final seconds are an end-of-contract frenzy (often >20k prints)
        # that the strategy never trades (exits finish by close-30s), so the
        # tape stops TAPE_END_BUFFER seconds before close.
        trades, truncated = self.trades(ticker, start, end - TAPE_END_BUFFER)
        record = {
            "ticker": ticker,
            "opens_at": m["open_time"],
            "closes_at": m["close_time"],
            "strike": m.get("floor_strike"),
            "result": m["result"],
            "settlement_ts": m.get("settlement_ts"),
            "candles": candles,
            "trades_compact": trades,
            "tape_window": [start, end - TAPE_END_BUFFER],
            "trades_truncated": truncated,
            "source": "Kalshi public REST (unauthenticated GET)",
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, separators=(",", ":")))
        tmp.replace(path)
        return path

    def run(self, count: int, before: str | None = None):
        markets = self.settled_markets(count, before)
        for i, m in enumerate(markets, 1):
            self.fetch_market(m)
            if i % 25 == 0:
                print(f"fetched {i}/{len(markets)}", flush=True)
        return len(markets)


def main():
    install_guard()
    p = argparse.ArgumentParser(description="Cache settled KXBTC15M public history")
    p.add_argument("--out", type=Path, default=Path("data/micro_scalp/historical"))
    p.add_argument("--markets", type=int, default=300)
    p.add_argument("--before", default=None, help="Only markets closing at/before this ISO time")
    a = p.parse_args()
    n = HistoricalFetcher(a.out).run(a.markets, a.before)
    print(f"cached {n} settled markets in {a.out}")


if __name__ == "__main__":
    main()

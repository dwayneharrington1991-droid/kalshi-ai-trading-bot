"""Record fresh public KXBTC15M data for forward (unseen) paper evaluation.

Writes one JSON object per line. Every record carries ``received_at``: the
local wall-clock time the data became available to us. The engine only lets a
record influence decisions at or after its ``received_at``, which is what keeps
replays free of look-ahead.

Record types
- ``market``     contract metadata (open/close time, strike)
- ``book``       full public order book depth (YES bids and NO bids)
- ``trade``      public trade prints (exchange ``created_time`` + receipt time)
- ``ai``         existing AI state read GET-only from a separate signals
                 dashboard. LIVE probability and the OPENING forecast are kept
                 in separate fields and never merged.
- ``settlement`` official result once Kalshi publishes it

Usage (never point --signals-url at a port you do not want polled):
    python -m src.micro_scalp.recorder --out data/micro_scalp/fresh.jsonl \
        --signals-url http://127.0.0.1:8790/api/state --minutes 60
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from . import install_guard
from .public_data import PublicGetClient

SERIES = "KXBTC15M"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_book(raw: dict) -> dict:
    fixed = raw.get("orderbook_fp")
    if fixed is not None:
        yes = [(float(p), float(q)) for p, q in fixed.get("yes_dollars") or []]
        no = [(float(p), float(q)) for p, q in fixed.get("no_dollars") or []]
    else:
        legacy = raw.get("orderbook") or {}
        yes = [(p / 100, float(q)) for p, q in legacy.get("yes") or []]
        no = [(p / 100, float(q)) for p, q in legacy.get("no") or []]
    yes = sorted(((p, q) for p, q in yes if q > 0), reverse=True)
    no = sorted(((p, q) for p, q in no if q > 0), reverse=True)
    return {"yes_bids": yes, "no_bids": no}


def extract_ai(state: dict) -> dict:
    """Copy existing AI outputs verbatim. Missing values stay None (never invented)."""
    pe = state.get("prediction_engine") or {}
    initial = pe.get("initial") or {}
    market = state.get("market") or {}
    opening = None
    if initial.get("generated_at") and initial.get("up") is not None:
        opening = {
            k: initial.get(k)
            for k in ("ticker", "generated_at", "data_cutoff", "up", "direction", "opening_delay_seconds")
        }
    return {
        "ticker": pe.get("ticker") or market.get("ticker"),
        "generated_at": state.get("generated_at"),
        "stale": bool(state.get("stale", True)),
        "live": {
            "up": pe.get("up"),
            "updated_at": pe.get("updated_at"),
            "data_cutoff": pe.get("data_cutoff"),
            "forecast_type": pe.get("forecast_type"),
            "status": pe.get("status"),
            "regime": pe.get("regime"),
            "confidence_status": (pe.get("confidence") or {}).get("status"),
        },
        "opening": opening,
        # BTC reference as published by the signals service (proxy; may be stale).
        "reference": {k: (state.get("reference") or {}).get(k) for k in ("price", "observed_at", "source")},
        "existing_scalp_call": state.get("scalp"),
        "existing_direction": state.get("direction"),
    }


class Recorder:
    def __init__(self, out: Path, signals_url: str | None, client: PublicGetClient | None = None):
        self.out = out
        self.signals_url = signals_url
        self.client = client or PublicGetClient()
        self.markets: dict[str, dict] = {}
        self.trade_ids: set[str] = set()
        self.last_trade_ts: dict[str, int] = {}
        self.pending_settlement: dict[str, float] = {}
        self.settled: set[str] = set()
        self.last_discovery = 0.0
        self.errors = 0
        self.failures: dict[str, int] = {}
        self.backoff_until: dict[str, float] = {}
        self.trade_every = 2  # trade tape every 2nd tick; book depth every tick
        out.parent.mkdir(parents=True, exist_ok=True)
        self.fh = out.open("a", encoding="utf-8")

    def write(self, record: dict):
        record.setdefault("received_at", now_iso())
        self.fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.fh.flush()

    def discover(self):
        data = self.client.kalshi("/markets", {"series_ticker": SERIES, "status": "open", "limit": 10})
        received = now_iso()
        for m in data.get("markets", []):
            t = m["ticker"]
            if t not in self.markets:
                meta = {
                    "type": "market",
                    "received_at": received,
                    "ticker": t,
                    "opens_at": m.get("open_time"),
                    "closes_at": m.get("close_time"),
                    "strike": m.get("floor_strike"),
                }
                self.markets[t] = meta
                self.write(meta)

    def active(self):
        now = datetime.now(timezone.utc)
        out = []
        for t, m in self.markets.items():
            try:
                o = datetime.fromisoformat(m["opens_at"].replace("Z", "+00:00"))
                c = datetime.fromisoformat(m["closes_at"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, ValueError):
                continue
            if o <= now < c:
                out.append(t)
            elif now >= c and t not in self.pending_settlement and t not in self.settled:
                self.pending_settlement[t] = 0.0
        return out

    def poll_book(self, ticker):
        raw = self.client.kalshi(f"/markets/{ticker}/orderbook")
        received = now_iso()
        self.write({"type": "book", "received_at": received, "ticker": ticker, **parse_book(raw)})

    def poll_trades(self, ticker):
        params = {"ticker": ticker, "limit": 200}
        if ticker in self.last_trade_ts:
            params["min_ts"] = self.last_trade_ts[ticker] - 2
        data = self.client.kalshi("/markets/trades", params)
        received = now_iso()
        for tr in reversed(data.get("trades", [])):
            tid = tr.get("trade_id")
            if not tid or tid in self.trade_ids:
                continue
            self.trade_ids.add(tid)
            created = tr.get("created_time")
            self.write(
                {
                    "type": "trade",
                    "received_at": received,
                    "ticker": ticker,
                    "trade_id": tid,
                    "created_time": created,
                    "yes_price": float(tr.get("yes_price_dollars")),
                    "count": float(tr.get("count_fp")),
                    "taker_side": tr.get("taker_side"),
                }
            )
            try:
                ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
                self.last_trade_ts[ticker] = max(self.last_trade_ts.get(ticker, 0), ts)
            except (AttributeError, ValueError):
                pass

    def poll_ai(self):
        if not self.signals_url:
            return
        state = self.client.get_json(self.signals_url)
        self.write({"type": "ai", "received_at": now_iso(), **extract_ai(state)})

    def poll_settlements(self):
        for ticker, last in list(self.pending_settlement.items()):
            if time.time() - last < 20:
                continue
            self.pending_settlement[ticker] = time.time()
            m = self.client.kalshi(f"/markets/{ticker}")["market"]
            if m.get("result") in ("yes", "no"):
                self.write(
                    {
                        "type": "settlement",
                        "received_at": now_iso(),
                        "ticker": ticker,
                        "result": m["result"],
                        "outcome": "up" if m["result"] == "yes" else "down",
                    }
                )
                del self.pending_settlement[ticker]
                self.settled.add(ticker)

    def _safe(self, name, fn, *args):
        """One failed endpoint never blocks the others; every gap is recorded."""
        if time.time() < self.backoff_until.get(name, 0):
            return
        try:
            fn(*args)
            self.failures[name] = 0
        except Exception as exc:  # keep recording; log the gap honestly
            self.errors += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            self.failures[name] = self.failures.get(name, 0) + 1
            delay = min(30, 2 ** self.failures[name]) if status == 429 else 0
            self.backoff_until[name] = time.time() + delay
            self.write(
                {
                    "type": "gap",
                    "received_at": now_iso(),
                    "source": name,
                    "error_type": type(exc).__name__,
                    "status": status,
                    "backoff_seconds": delay,
                }
            )

    def step(self, tick: int = 0):
        if time.time() - self.last_discovery > 15:
            self.last_discovery = time.time()
            self._safe("discover", self.discover)
        for ticker in self.active():
            self._safe("book", self.poll_book, ticker)
            if tick % self.trade_every == 0:
                self._safe("trades", self.poll_trades, ticker)
        self._safe("ai", self.poll_ai)
        self._safe("settlement", self.poll_settlements)

    def run(self, minutes: float, interval: float = 1.0):
        end = time.time() + minutes * 60
        tick = 0
        while time.time() < end or (self.pending_settlement and time.time() < end + 600):
            started = time.time()
            self.step(tick)
            tick += 1
            time.sleep(max(0.0, interval - (time.time() - started)))
        self.fh.close()


def main():
    install_guard()
    p = argparse.ArgumentParser(description="Record fresh public KXBTC15M data (GET-only, no credentials)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--signals-url", default=None, help="Optional GET-only URL of a separate signals dashboard /api/state")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--interval", type=float, default=1.0)
    a = p.parse_args()
    Recorder(a.out, a.signals_url).run(a.minutes, a.interval)


if __name__ == "__main__":
    main()

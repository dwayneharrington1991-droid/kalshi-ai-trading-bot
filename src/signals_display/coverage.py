"""Bounded independent public-data collectors; no auth or trading endpoints."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math

from .model import number, timestamp
from .order_flow import PublicOrderFlow
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree


def now_utc():
    return datetime.now(timezone.utc)


def kraken_spot(read):
    ticker = read("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
    if ticker.get("error"):
        raise ValueError("Kraken ticker error")
    raw = next(iter(ticker["result"].values()))
    bid, ask = number(float(raw["b"][0]), 1, 1e9), number(float(raw["a"][0]), 1, 1e9)
    if bid > ask or (ask - bid) / bid > 0.02:
        raise ValueError("crossed/illiquid BTC/USD quote")
    quote_at = now_utc().isoformat()
    candles = read("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1")
    if candles.get("error"):
        raise ValueError("Kraken candle error")
    bars = next(v for k, v in candles["result"].items() if k != "last")
    book = read("https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=10")
    levels = next(iter(book["result"].values()))
    bids = sum(number(float(q[1]), 0, 1e12) for q in levels["bids"])
    asks = sum(number(float(q[1]), 0, 1e12) for q in levels["asks"])
    return dict(
        name="Kraken BTC/USD spot",
        currency="USD",
        price_convention="bid/ask midpoint",
        price=(bid + ask) / 2,
        bid=bid,
        ask=ask,
        spread=ask - bid,
        observed_at=quote_at,
        event_time=None,
        latency_note="receipt time; last-trade event time unavailable",
        depth_imbalance=(bids - asks) / (bids + asks) if bids + asks else None,
        depth_bid_btc=bids,
        depth_ask_btc=asks,
        candles=[
            dict(start=str(b[0]), open=b[1], high=b[2], low=b[3], close=b[4], volume=b[6])
            for b in bars
        ],
        candle_source="Kraken BTC/USD public OHLC",
        candle_received_at=now_utc().isoformat(),
    )


def kraken_derivatives(read):
    data = read("https://futures.kraken.com/derivatives/api/v3/tickers")
    event = timestamp(data["serverTime"])
    if abs((now_utc() - event).total_seconds()) > 30:
        raise ValueError("stale/future derivative server time")
    raw = next(t for t in data["tickers"] if t["symbol"] == "PI_XBTUSD")
    mark = number(float(raw["markPrice"]), 1, 1e9)
    index = number(float(raw["indexPrice"]), 1, 1e9)
    funding = float(raw["fundingRate"]) if raw.get("fundingRate") is not None else None
    if funding is not None and not math.isfinite(funding):
        raise ValueError("invalid funding")
    return dict(
        name="Kraken inverse BTC/USD perpetual PI_XBTUSD",
        currency="USD",
        price=mark,
        mark=mark,
        index=index,
        basis_fraction=mark / index - 1,
        funding_rate_raw=funding,
        funding_interval="not inferred; raw provider convention",
        open_interest_contracts=number(float(raw["openInterest"]), 0, 1e15),
        open_interest_unit="inverse contracts, not BTC or USD notional",
        observed_at=now_utc().isoformat(),
        event_time=event.isoformat(),
        model_role="context only; predictive effect not validated",
    )


def public_news(read_text):
    root = ElementTree.fromstring(read_text("https://www.federalreserve.gov/feeds/press_all.xml"))
    items = []
    now = now_utc()
    for item in root.findall(".//item")[:40]:
        try:
            at = parsedate_to_datetime(item.findtext("pubDate", ""))
            if at.tzinfo is None or at > now:
                continue
            title = (item.findtext("title", "") or "")[:240]
            items.append({"published_at": at.isoformat(), "headline_untrusted": title})
        except (ValueError, TypeError):
            continue
    return {
        "name": "Federal Reserve public press-release RSS (macro context, not universal BTC news)",
        "observed_at": now.isoformat(),
        "items": items,
        "model_role": "Untrusted text is never interpreted as instructions; timestamp count is a candidate feature, not sentiment or a proven signal.",
    }


class FeedHub:
    def __init__(self, read, read_text=None):
        self.read = read
        self.read_text = read_text
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="public-btc-feeds")
        self.jobs = {}
        self.feeds = {}
        self.last_started = {}
        self.previous_oi = None
        self.order_flow = PublicOrderFlow()

    def snapshot(self, now):
        collectors = {"Kraken spot": kraken_spot, "Kraken derivatives": kraken_derivatives}
        if self.read_text:
            collectors["Federal Reserve public releases"] = lambda _: public_news(self.read_text)
        for name, collect in collectors.items():
            job = self.jobs.get(name)
            if job and job.done():
                try:
                    feed = job.result()
                    if name == "Kraken derivatives":
                        if self.previous_oi and self.previous_oi > 0:
                            feed["open_interest_change_fraction"] = (
                                feed["open_interest_contracts"] / self.previous_oi - 1
                            )
                        self.previous_oi = feed["open_interest_contracts"]
                    self.feeds[name] = {"status": "CONNECTED", "data": feed}
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    self.feeds[name] = {
                        "status": "UNAVAILABLE",
                        "reason": type(exc).__name__
                        + (f" HTTP {status}" if status else "")
                        + "; no authentication fallback",
                    }
                del self.jobs[name]
            last = self.last_started.get(name)
            if name not in self.jobs and (
                last is None
                or (now - last).total_seconds()
                >= (60 if name == "Federal Reserve public releases" else 5)
            ):
                self.jobs[name] = self.executor.submit(collect, self.read)
                self.last_started[name] = now
        result = {
            name: dict(
                self.feeds.get(
                    name, {"status": "CONNECTING", "reason": "First public request pending"}
                )
            )
            for name in collectors
        }
        for name, feed in result.items():
            if feed.get("data"):
                age = (now - timestamp(feed["data"]["observed_at"])).total_seconds()
                feed["age_seconds"] = round(age, 2)
                if not 0 <= age <= (180 if "releases" in name else 15):
                    feed["status"] = "DELAYED"
                    feed[
                        "reason"
                    ] = "Quote receipt older than 15s or future-dated; excluded from features"
        result["Binance BTC/USDT"] = {
            "status": "EXCLUDED",
            "reason": "No verified contemporaneous USDT/USD conversion feed; not treated as USD or an independent vote. No Binance geographic access claim.",
        }
        result["Official CF Benchmarks BRTI"] = {
            "status": "UNAVAILABLE",
            "reason": "Official target/rules metadata connected through Kalshi; no legitimate live index stream configured",
        }
        flow = self.order_flow.snapshot(now)
        result["Kraken public order flow"] = {
            "status": "CONNECTED"
            if any(flow[k]["status"] == "LIVE" for k in ("spot", "perpetual", "spot_book"))
            else "WARMING_UP_OR_UNAVAILABLE",
            "data": flow,
            "reason": "Public WebSocket CVD and checksum-verified top-20 spot depth; experimental context only",
        }
        return result

    def close(self):
        self.order_flow.close()
        self.executor.shutdown(wait=False, cancel_futures=True)

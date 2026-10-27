"""Credential-free public GET adapter, replay engine, and simulation journal.

No trading SDK, settings, environment loading, private API, or order operations.
"""
from __future__ import annotations
from .storage import connect as storage_connect

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from time import perf_counter, monotonic
from urllib.parse import urlencode, quote

import httpx

from .coverage import FeedHub
from . import prediction_engine
from . import scalp_paper
from . import edge_scanner
from .model import Display, evaluate, fee, number, timestamp
from .edges import assess_edges
from .reliability import metrics, wilson

UTC = timezone.utc
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def utcnow():
    return datetime.now(UTC)


class PublicFeed:
    """Only fixed public market-data GETs; never reads trading credentials."""

    def __init__(self, get=None, multi_venue=None):
        self.get = get or httpx.get
        self.transport_diagnostics = []
        self.candle_cache = None
        self.candles_checked = None
        from concurrent.futures import ThreadPoolExecutor

        self.candle_pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-candles")
            if get is None
            else None
        )
        self.candle_job = None
        self.candle_started = None
        self.kalshi_stream = None
        self.streams = None
        self.http_backoff = {}
        self.hub = (
            FeedHub(self._read, self._read_text)
            if (get is None if multi_venue is None else multi_venue)
            else None
        )
        if get is None:
            from .streaming import StreamHub

            self.streams = StreamHub(order_flow=self.hub.order_flow if self.hub else None)
        if self.candle_pool is not None:
            self.sampled_candles(utcnow())

    def _read(self, url):
        started = perf_counter()
        diagnostic = {
            "endpoint": url.split("?")[0],
            "exchange_event_latency": "unknown; receipt timestamps only",
        }
        try:
            retry = self.http_backoff.get(diagnostic["endpoint"], {})
            if monotonic() < retry.get("until", 0):
                diagnostic["retry_after_seconds"] = round(retry["until"] - monotonic(), 2)
                raise ValueError("PUBLIC_HTTP_BACKOFF: waiting for provider retry interval")
            response = self.get(
                url,
                timeout=4,
                follow_redirects=False,
                headers={"Accept": "application/json", "Cache-Control": "no-cache"},
            )
            diagnostic["http_status"] = response.status_code
            if response.status_code == 429 or response.status_code >= 500:
                attempts = retry.get("attempts", 0) + 1
                delay = min(60, 2 ** min(attempts, 6))
                try:
                    delay = max(delay, min(120, float(response.headers.get("Retry-After", "0"))))
                except ValueError:
                    pass
                self.http_backoff[diagnostic["endpoint"]] = {
                    "until": monotonic() + delay,
                    "attempts": attempts,
                }
                diagnostic["retry_after_seconds"] = delay
            from email.utils import parsedate_to_datetime

            try:
                date = parsedate_to_datetime(response.headers["Date"])
                diagnostic["server_date"] = date.isoformat()
                diagnostic["server_clock_difference_seconds"] = round(
                    (date - utcnow()).total_seconds(), 3
                )
            except (KeyError, ValueError, TypeError):
                pass
            try:
                cache_age = float(response.headers["Age"]) if "Age" in response.headers else None
                diagnostic["http_cache_age_seconds"] = (
                    cache_age if cache_age is not None and math.isfinite(cache_age) else None
                )
                if (
                    cache_age is not None
                    and cache_age > 5
                    and (url == COINBASE or "/orderbook" in url)
                ):
                    diagnostic["failure_reason"] = "STALE_HTTP_CACHE"
                    raise ValueError("STALE_HTTP_CACHE: live response Age exceeds five seconds")
            except (TypeError, OverflowError):
                pass
            response.raise_for_status()
            value = response.json()
            diagnostic["status"] = "OK"
            self.http_backoff.pop(diagnostic["endpoint"], None)
            return value
        except Exception as exc:
            diagnostic["status"] = "FAILED"
            diagnostic["error_type"] = type(exc).__name__
            if isinstance(exc, httpx.TransportError):
                previous = self.http_backoff.get(diagnostic["endpoint"], {})
                attempts = previous.get("attempts", 0) + 1
                delay = min(60, 2 ** min(attempts, 6))
                self.http_backoff[diagnostic["endpoint"]] = {
                    "until": monotonic() + delay,
                    "attempts": attempts,
                }
                diagnostic["retry_after_seconds"] = delay
            raise
        finally:
            diagnostic.update(
                round_trip_ms=round((perf_counter() - started) * 1000, 2),
                received_at=utcnow().isoformat(),
            )
            self.transport_diagnostics.append(diagnostic)
            self.transport_diagnostics = self.transport_diagnostics[-20:]

    def sampled_candles(self, now):
        if self.candle_pool is None:
            return self.candles(now)
        if self.candle_job and self.candle_job.done():
            try:
                result = self.candle_job.result()
                if isinstance(result, dict):
                    self.candle_cache = result
            except Exception as exc:
                self.candle_cache = {
                    **(self.candle_cache or {}),
                    "error": "CANDLE_FETCH_FAILED: " + type(exc).__name__,
                }
            self.candle_job = None
        if self.candle_job is None and (
            self.candle_started is None or (now - self.candle_started).total_seconds() >= 60
        ):
            self.candle_started = now
            self.candle_job = self.candle_pool.submit(self.candles, now)
        return self.candle_cache or {}

    def _read_text(self, url):
        # Fixed public RSS source only; never arbitrary user URLs or auth headers.
        if url != "https://www.federalreserve.gov/feeds/press_all.xml":
            raise ValueError("Unsupported public news source")
        response = self.get(
            url,
            timeout=10,
            follow_redirects=False,
            headers={"Accept": "application/rss+xml, application/xml"},
        )
        response.raise_for_status()
        if len(response.content) > 262144:
            raise ValueError("Public feed exceeds size limit")
        return response.text

    def contract_quote(self, ticker):
        if not ticker.startswith("KXBTC15M-"):
            raise ValueError("Only KXBTC15M public quotes are supported")
        raw = self._read(KALSHI + "/markets/" + quote(ticker, safe=""))["market"]
        book = self._read(KALSHI + "/markets/" + quote(ticker, safe="") + "/orderbook")
        fixed = book.get("orderbook_fp")
        if fixed is not None:
            yes = [
                (number(float(p), 0, 1), number(float(q), 0, 1e12))
                for p, q in fixed.get("yes_dollars", [])
            ]
            no = [
                (number(float(p), 0, 1), number(float(q), 0, 1e12))
                for p, q in fixed.get("no_dollars", [])
            ]
        else:
            legacy = book.get("orderbook", {})
            yes = [
                (number(p, 0, 100) / 100, number(q, 0, 1e12)) for p, q in (legacy.get("yes") or [])
            ]
            no = [
                (number(p, 0, 100) / 100, number(q, 0, 1e12)) for p, q in (legacy.get("no") or [])
            ]
        yes = [v for v in yes if v[1] > 0]
        no = [v for v in no if v[1] > 0]
        if not yes or not no:
            raise ValueError("No executable two-sided recorded-contract book")
        y, n = max(yes), max(no)
        series = self.last_series
        return dict(
            ticker=ticker,
            observed_at=utcnow().isoformat(),
            closes_at=raw["close_time"],
            fee_type=series.get("fee_type"),
            fee_multiplier=series.get("fee_multiplier"),
            book_diagnostic=None,
            quotes={
                "up": dict(bid=y[0], ask=1 - n[0], bid_size=y[1], ask_size=n[1]),
                "down": dict(bid=n[0], ask=1 - y[0], bid_size=n[1], ask_size=y[1]),
            },
        )

    def candles(self, now):
        if self.candles_checked is not None and (now - self.candles_checked).total_seconds() < 60:
            return self.candle_cache or {}
        self.candles_checked = now
        cutoff = int(now.timestamp()) // 60 * 60
        try:
            merged = {
                int(b["start"]): b
                for b in (self.candle_cache or {}).get("candles", [])
                if cutoff - 86400 <= int(b["start"]) and int(b["start"]) + 60 <= cutoff
            }
            missing = [t for t in range(cutoff - 86400, cutoff, 60) if t not in merged]
            ranges = []
            for stamp in missing:
                if ranges and stamp == ranges[-1][1] and ranges[-1][1] - ranges[-1][0] < 288 * 60:
                    ranges[-1][1] += 60
                else:
                    ranges.append([stamp, stamp + 60])
            if not ranges:
                return self.candle_cache or {}
            for left, right in ranges[:5]:
                start = left - 60
                end = min(int(utcnow().timestamp()), right + 60)
                url = (
                    "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles?"
                    + urlencode(
                        {"start": start, "end": end, "granularity": "ONE_MINUTE", "limit": 300}
                    )
                )
                for bar in self._read(url)["candles"]:
                    stamp = int(bar["start"])
                    if not cutoff - 86400 <= stamp or stamp + 60 > cutoff:
                        continue
                    if stamp in merged and any(
                        float(bar[k]) != float(merged[stamp][k])
                        for k in ("open", "high", "low", "close", "volume")
                    ):
                        raise ValueError("Conflicting overlapping history candles")
                    merged[stamp] = bar
            received = utcnow()
            self.candle_cache = {
                "candles": [merged[t] for t in sorted(merged) if t + 60 <= received.timestamp()],
                "source": "Coinbase BTC-USD public 24h one-minute candles (proxy)",
                "received_at": received.isoformat(),
                "refresh": {
                    "method": "Initial 24h preload, then missing closed minutes only",
                    "requests": min(5, len(ranges)),
                    "retained_bars": len(merged),
                    "maximum_requests_per_refresh": 5,
                },
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return {
                "error": "MISSING_CANDLES: public one-minute candle endpoint unavailable",
                **(self.candle_cache or {}),
            }
        return self.candle_cache

    def resolved(self, ticker):
        data = self._read(KALSHI + "/markets/" + quote(ticker, safe=""))
        market = data.get("market", {})
        return market.get("result") if market.get("status") in {"settled", "finalized"} else None

    def enriched(self, payload, now):
        """Combine current streaming observations with unchanged REST metadata.

        Receipts retain their original times. An event never advances an older
        quote/reference timestamp or turns a metadata response into a fresh book.
        """
        result = {
            **payload,
            # The cached REST payload can be reused between polls while newer
            # socket receipts arrive. Processing must use the current cycle
            # clock or those valid receipts look future-dated and get rejected.
            # Individual REST/stream observations retain their own immutable
            # receipt timestamps below.
            "clock": now.isoformat(),
            "now": now.isoformat(),
            "coverage": dict(payload.get("coverage", {})),
        }
        result["markets"] = [dict(m) for m in payload.get("markets", [])]
        result["stream_observations"] = []
        diagnostics = {"processing_target_ms": 500, "actual_exchange_cadence": "Provider-dependent"}
        diagnostics["rest"] = {
            "status": "FAILED" if payload.get("rest_poll_failed") else "CONNECTED",
            "error_type": payload.get("rest_error_type"),
            "note": "REST fallback never advances a cached quote timestamp",
        }
        if self.streams is not None:
            streamed = self.streams.snapshot(now)
            diagnostics["public"] = streamed.get("diagnostics", {})
            result["coverage"].update(streamed.get("feeds", {}))
            candidate = streamed.get("reference") or {}
            try:
                if (
                    candidate.get("price") is not None
                    and 0 <= (now - timestamp(candidate["observed_at"])).total_seconds() <= 5
                ):
                    result["reference"] = candidate
            except (KeyError, ValueError, TypeError, AttributeError):
                pass
            result["stream_observations"] = self.streams.drain_observations()
        for market in result["markets"]:
            if self.kalshi_stream is None:
                continue
            book = self.kalshi_stream.snapshot(market["ticker"], now)
            if book.get("status") == "LIVE" and book.get("quotes"):
                market.update(
                    quotes=book["quotes"],
                    observed_at=book["observed_at"],
                    quote_event_time=book.get("event_time"),
                    quote_source="Kalshi authenticated read-only WebSocket order book",
                    book_diagnostic=None,
                )
            diagnostics.setdefault("kalshi", {})[market["ticker"]] = {
                k: v for k, v in book.items() if k not in {"quotes", "levels"}
            }
        if self.kalshi_stream is None:
            diagnostics["kalshi"] = {
                "status": "REST_FALLBACK",
                "reason": "Kalshi book streaming requires a verified read-only connection; public book REST remains available, normally once per second",
            }
        result["streaming"] = diagnostics
        return result

    def poll(self):
        markets, cursor, seen = [], None, set()
        discovery_time = int(utcnow().timestamp())
        for _ in range(20):
            # Official close-time filters are compatible with an omitted status,
            # not status=open. The static open-only page can lag a rollover even
            # while a direct market reports active. Select active timestamps
            # ourselves and never infer a ticker or an unpublished threshold.
            params = {
                "series_ticker": "KXBTC15M",
                "min_close_ts": discovery_time,
                "max_close_ts": discovery_time + 1800,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            page = self._read(KALSHI + "/markets?" + urlencode(params))
            markets.extend(page["markets"])
            cursor = page.get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Repeated public pagination cursor")
            seen.add(cursor)
        else:
            raise ValueError("Incomplete market discovery; refusing partial selection")
        series = self._read(KALSHI + "/series/KXBTC15M")["series"]
        self.last_series = series
        now = utcnow()
        result = []
        sources = series.get("settlement_sources", [])
        source = "; ".join(str(item.get("name", "")) for item in sources) or "unavailable"
        for raw in markets:
            if raw.get("status", "open") not in {"open", "active"}:
                continue
            if raw.get("series_ticker", "KXBTC15M") != "KXBTC15M":
                continue
            if not str(raw.get("ticker", "")).startswith("KXBTC15M-"):
                continue
            # Never guess the mapping of YES to UP from a ticker or title.
            orientation = raw.get("strike_type") in {"greater", "greater_or_equal", "above"}
            close = timestamp(raw["close_time"])
            start = max(timestamp(raw["open_time"]), close - timedelta(minutes=15))

            def price(name):
                return number(float(raw[name + "_dollars"]), 0, 1)

            quotes = {}
            try:
                quotes = {
                    "up": {
                        "bid": price("yes_bid"),
                        "ask": price("yes_ask"),
                        "bid_size": 0,
                        "ask_size": 0,
                    },
                    "down": {
                        "bid": price("no_bid"),
                        "ask": price("no_ask"),
                        "bid_size": 0,
                        "ask_size": 0,
                    },
                }
            except (KeyError, ValueError, TypeError):
                pass
            result.append(
                dict(
                    ticker=raw["ticker"],
                    series="KXBTC15M",
                    status="open",
                    opens_at=start.isoformat(),
                    closes_at=close.isoformat(),
                    observed_at=now.isoformat(),
                    strike=raw.get("floor_strike"),
                    metadata_received_at=now.isoformat(),
                    settlement_reference=source,
                    up_definition="YES according to published contract settlement rules"
                    if orientation
                    else "unknown",
                    rules=raw.get("rules_primary", ""),
                    quotes=quotes,
                    fee_type=series.get("fee_type"),
                    fee_multiplier=series.get("fee_multiplier"),
                    mapping_verified=orientation,
                    comparison_operator=">="
                    if orientation and "at least" in str(raw.get("rules_primary", "")).lower()
                    else None,
                    averaging_seconds=60,
                    averaging_rule_verified=(
                        orientation
                        and "CF Benchmarks" in source
                        and str(raw.get("rules_primary", ""))
                        .lower()
                        .count("simple average of the sixty seconds")
                        == 2
                        and "at least" in str(raw.get("rules_primary", "")).lower()
                        and "brti" in str(raw.get("rules_primary", "")).lower()
                    ),
                )
            )
        selected = active_market({"markets": result}, now)
        streaming_book = None
        if selected and self.kalshi_stream is not None:
            candidate = self.kalshi_stream.snapshot(selected["ticker"], now)
            if candidate.get("status") == "LIVE" and candidate.get("quotes"):
                streaming_book = candidate
                selected.update(
                    quotes=candidate["quotes"],
                    observed_at=candidate["observed_at"],
                    quote_event_time=candidate.get("event_time"),
                    book_diagnostic=None,
                    quote_source="Kalshi authenticated read-only WebSocket order book",
                )
        if selected and selected["mapping_verified"] and streaming_book is None:
            selected[
                "book_diagnostic"
            ] = "MISSING_DEPTH: public orderbook has an empty side or unsupported schema"
            try:
                book = self._read(
                    KALSHI + "/markets/" + quote(selected["ticker"], safe="") + "/orderbook"
                )
                if "orderbook_fp" in book:
                    fixed = book["orderbook_fp"]
                    yes = [
                        (number(float(p), 0, 1), number(float(q), 0, 1e12))
                        for p, q in fixed.get("yes_dollars", [])
                    ]
                    no = [
                        (number(float(p), 0, 1), number(float(q), 0, 1e12))
                        for p, q in fixed.get("no_dollars", [])
                    ]
                else:
                    legacy = book.get("orderbook", {})
                    yes = [
                        (number(p, 0, 100) / 100, number(q, 0, 1e12))
                        for p, q in (legacy.get("yes") or [])
                    ]
                    no = [
                        (number(p, 0, 100) / 100, number(q, 0, 1e12))
                        for p, q in (legacy.get("no") or [])
                    ]
                yes = [level for level in yes if level[1] > 0]
                no = [level for level in no if level[1] > 0]
                if yes and no:
                    selected["book_diagnostic"] = None
                    selected["observed_at"] = utcnow().isoformat()
                    y, n = max(yes), max(no)
                    selected["quotes"] = {
                        "up": dict(bid=y[0], ask=round(1 - n[0], 6), bid_size=y[1], ask_size=n[1]),
                        "down": dict(
                            bid=n[0], ask=round(1 - y[0], 6), bid_size=n[1], ask_size=y[1]
                        ),
                    }
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                selected["book_diagnostic"] = (
                    "MISSING_DEPTH: public orderbook unavailable ("
                    + type(exc).__name__
                    + (" HTTP " + str(status) if status else "")
                    + ")"
                )
                # Summary quotes remain visible, but they never imply known executable depth.
        reference = {
            "price": None,
            "source": "Coinbase BTC-USD spot (proxy, NOT settlement index)",
            "observed_at": None,
            "official": False,
        }
        warnings = [
            "Coinbase is a proxy, not the official settlement index. Model estimates are UNVALIDATED."
        ]
        streamed = self.streams.snapshot(utcnow()).get("reference") if self.streams else None
        if streamed:
            reference = streamed
        else:
            try:
                spot = self._read(COINBASE)["data"]
                if spot.get("currency") != "USD" or spot.get("base") != "BTC":
                    raise ValueError("unexpected reference currency")
                reference.update(
                    price=number(float(spot["amount"]), 0.000001, 1e12),
                    observed_at=utcnow().isoformat(),
                )
            except (httpx.HTTPError, KeyError, ValueError, TypeError):
                warnings.append(
                    "Public reference proxy unavailable; official settlement feed not connected."
                )
        return {
            "markets": result,
            "reference": reference,
            "warnings": warnings,
            "mode": "PUBLIC MARKET DATA",
            "evidence": {},
            "btc_candles": self.sampled_candles(utcnow()),
            "transport_diagnostics": list(self.transport_diagnostics),
            "coverage": self.hub.snapshot(utcnow()) if self.hub else {},
        }


class ReplayFeed:
    def __init__(self, path):
        self.frames = json.loads(Path(path).read_text())
        if not isinstance(self.frames, list) or not self.frames:
            raise ValueError("Replay must be a nonempty list of frames")
        times = [timestamp(frame["now"]) for frame in self.frames]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("Replay timestamps must increase")
        self.index = 0
        self.last_wall = utcnow()

    def poll(self):
        index = min(self.index, len(self.frames) - 1)
        frame = self.frames[index]
        now = timestamp(frame["now"])
        if self.index >= len(self.frames):
            now += utcnow() - self.last_wall
        else:
            self.last_wall = utcnow()
        self.index += 1
        return {**frame, "mode": "REPLAY / SIMULATED", "clock": now.isoformat()}


def active_market(payload, now):
    candidates = []
    for market in payload.get("markets", []):
        if market.get("series") != "KXBTC15M" or market.get("status") != "open":
            continue
        start, end = timestamp(market["opens_at"]), timestamp(market["closes_at"])
        if 0 < (end - start).total_seconds() <= 900 and start <= now < end:
            candidates.append(market)
    return candidates[0] if len(candidates) == 1 else None


def levels(entry, multiplier, target, stop):
    """Hypothetical exit bids for requested net thresholds, including both fees."""
    prices = [
        (cent / 100, cent / 100 - entry - fee(entry, multiplier) - fee(cent / 100, multiplier))
        for cent in range(101)
    ]
    targets = [bid for bid, pnl in prices if pnl >= target - 1e-10]
    stops = [bid for bid, pnl in prices if pnl <= -stop + 1e-10]
    return (min(targets) if targets else None, max(stops) if stops else None)


def analyze(payload, now, target=0.05, stop=0.03, horizon=60, model_result=None, model_info=None):
    market = active_market(payload, now)
    result = (
        model_result
        if model_result is not None
        else evaluate(payload, now=now, target=target, stop=stop, horizon=horizon)
    )
    if market and not market.get("mapping_verified", True):
        result = Display("WAIT", "YES/UP settlement mapping unverified")
    stale = True
    if market:
        age = (now - timestamp(market["observed_at"])).total_seconds()
        stale = not 0 <= age <= 5
    direction = "UNCLEAR"
    if result.settlement_up is not None and not stale:
        sensitivity = (model_info or {}).get(
            "settlement_probability_sensitivity", [result.settlement_up, result.settlement_up]
        )
        if result.settlement_up >= 0.6 and sensitivity[0] >= 0.6:
            direction = "UP"
        elif result.settlement_up <= 0.4 and sensitivity[1] <= 0.4:
            direction = "DOWN"
    plans = {}
    if market and not stale and market.get("mapping_verified", True):
        try:
            multiplier = number(market["fee_multiplier"], 0, 100)
            if market["fee_type"] not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("unknown fees")
            for side in ("up", "down"):
                bid = number(market["quotes"][side]["bid"], 0, 1)
                entry = number(market["quotes"][side]["ask"], max(0.000001, bid), 0.999999)
                target_bid, stop_bid = levels(entry, multiplier, target, stop)
                plans[side] = dict(
                    entry=entry,
                    target_bid=target_bid,
                    stop_bid=stop_bid,
                    net_target=target,
                    net_stop=stop,
                    holding_seconds=horizon,
                    probability=getattr(result, "scalp_" + side),
                    multiplier=multiplier,
                    entry_fee=fee(entry, multiplier),
                )
        except (KeyError, ValueError, TypeError):
            plans = {}
    available = [
        (side, plan["probability"])
        for side, plan in plans.items()
        if plan["probability"] is not None
        and plan["probability"] >= 0.6
        and plan["target_bid"] is not None
    ]
    available.sort(key=lambda item: item[1], reverse=True)
    scalp = "WAIT"
    if (
        result.status != "WAIT"
        and available
        and (len(available) == 1 or available[0][1] > available[1][1])
    ):
        scalp = available[0][0].upper() + " SCALP"
    scalp_lean = (
        ("SCALP " + available[0][0].upper())
        if available and (len(available) == 1 or available[0][1] > available[1][1])
        else "WAIT"
    )
    edges = {}
    if model_info and model_info.get("enabled") and market and not stale:
        edges = assess_edges(market, result, model_info, now)
        scalp = edges["scalp_call"]
        for side, plan in plans.items():
            execution = model_info.get("execution_assumptions", {})
            slip = execution.get("slippage_per_leg", 0.01)
            delay = execution.get("manual_delay_seconds", 3)
            cap = plan["entry"] + slip
            if cap >= 1:
                continue
            plan.update(
                display_ask=plan["entry"],
                entry=cap,
                entry_fee=fee(cap, plan["multiplier"]),
                manual_delay=delay,
                slippage=slip,
                model_version=model_info.get("version"),
            )
            target_bid, stop_bid = levels(cap, plan["multiplier"], target, stop)
            plan["target_bid"] = (
                target_bid + slip if target_bid is not None and target_bid + slip <= 1 else None
            )
            plan["stop_bid"] = (
                stop_bid + slip if stop_bid is not None and stop_bid + slip <= 1 else None
            )
    overall_reason = (model_info or {}).get("overall_reason", result.reason)
    scalp_reason = (model_info or {}).get("scalp_reason", result.reason)
    if stale:
        overall_reason = (
            scalp_reason
        ) = "STALE_OR_MISSING_CONTRACT: fresh unique contract snapshot required"
    elif result.settlement_up is not None and direction == "UNCLEAR":
        overall_reason += (
            "; UNCLEAR: probability/basis sensitivity does not support a 60% directional lean"
        )
    if (
        not stale
        and result.scalp_up is not None
        and result.scalp_down is not None
        and scalp == "WAIT"
    ):
        scalp_reason += "; WAIT: no unique side at or above 60% with a feasible target"
    if edges:
        scalp_reason += "; " + (
            "; ".join(
                side.upper() + ": " + data["reason"]
                for side, data in edges.get("scalp", {}).items()
            )
            or "WAIT: cost-adjusted scalp estimates unavailable"
        )
    comparison = {}
    if market and not stale:
        q = market.get("quotes", {}).get("up", {})
        if q.get("bid") is not None and q.get("ask") is not None:
            implied = (q["bid"] + q["ask"]) / 2
            comparison = {
                "market_implied_up": implied,
                "market_implied_down": 1 - implied,
                "market_bid_ask_range": [q["bid"], q["ask"]],
                "model_minus_market": result.settlement_up - implied
                if result.settlement_up is not None
                else None,
                "note": "Market-implied quote price, not a calibrated win probability; not a settlement model input.",
            }
    return dict(
        market_comparison=comparison,
        transport_diagnostics=payload.get("transport_diagnostics", []),
        edges=edges,
        scalp_lean=scalp_lean,
        settlement_qualification="WAIT" if edges else "UNVERIFIED",
        model=model_info or {"version": "uploaded-scenarios", "validated": False},
        now=now.isoformat(),
        mode=payload.get("mode", "REPLAY / SIMULATED"),
        market=market,
        reference=payload.get("reference", {}),
        stale=stale,
        remaining_seconds=max(0, (timestamp(market["closes_at"]) - now).total_seconds())
        if market
        else 0,
        direction=direction,
        scalp=scalp,
        estimate=asdict(result),
        plans=plans,
        warnings=payload.get("warnings", []),
        explanation=overall_reason,
        scalp_explanation=scalp_reason,
    )


class Journal:
    """Append-only predictions and explicitly counterfactual one-contract probes."""

    def __init__(self, path):
        self.path = str(path)
        self._last_candle_batch = None
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY, mode TEXT, ticker TEXT, timestamp TEXT,
                    direction TEXT, scalp TEXT, probability_up REAL, probability_down REAL,
                    payload TEXT, settlement_up INTEGER, UNIQUE(mode,ticker,timestamp));
                CREATE TABLE IF NOT EXISTS simulations (
                    id INTEGER PRIMARY KEY, prediction_id INTEGER, mode TEXT, ticker TEXT,
                    side TEXT, started_at TEXT, last_at TEXT, deadline TEXT,
                    entry REAL, entry_fee REAL, multiplier REAL, target REAL, stop REAL,
                    last_bid REAL, status TEXT, exit_at TEXT, exit_bid REAL, exit_fee REAL,
                    net_pnl REAL, real_fill INTEGER NOT NULL DEFAULT 0 CHECK(real_fill=0),
                    probability REAL, UNIQUE(prediction_id,side));
            """
            )

            columns = {row[1] for row in db.execute("PRAGMA table_info(simulations)")}
            additions = {
                "eligible_at": "TEXT",
                "entry_cap": "REAL",
                "manual_delay": "INTEGER NOT NULL DEFAULT 0",
                "slippage": "REAL NOT NULL DEFAULT 0",
                "holding_seconds": "INTEGER",
                "model_version": "TEXT NOT NULL DEFAULT 'legacy-immediate-probe'",
                "execution_at": "TEXT",
                "call_at_prediction": "TEXT",
            }
            for name, spec in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE simulations ADD COLUMN {name} {spec}")
            prediction_columns = {r[1] for r in db.execute("PRAGMA table_info(predictions)")}
            for name, spec in {
                "settlement_origin": "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                "settled_at": "TEXT",
                "outcome_last_attempt": "TEXT",
            }.items():
                if name not in prediction_columns:
                    db.execute(f"ALTER TABLE predictions ADD COLUMN {name} {spec}")
            prediction_engine.create(db)
            scalp_paper.create_schema(db)
            edge_scanner.create_schema(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS forecast_outcomes (ticker TEXT,origin TEXT,observed_at TEXT,outcome INTEGER,PRIMARY KEY(ticker,origin))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS feed_observations (source TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(source,timestamp))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT,PRIMARY KEY(source,start))"
            )
            db.executescript(
                """CREATE INDEX IF NOT EXISTS predictions_ticker_mode ON predictions(ticker,mode);
                CREATE INDEX IF NOT EXISTS predictions_mode_id ON predictions(mode,id DESC);
                CREATE INDEX IF NOT EXISTS simulations_pending ON simulations(status);
                CREATE INDEX IF NOT EXISTS simulations_probe ON simulations(mode,ticker,side,id DESC);
                CREATE INDEX IF NOT EXISTS simulations_prediction ON simulations(prediction_id,id DESC);
                CREATE INDEX IF NOT EXISTS btc_candles_start ON btc_candles(start DESC);
                CREATE TABLE IF NOT EXISTS settlement_queue (
                    mode TEXT,ticker TEXT,closes_at TEXT,last_attempt TEXT,resolved INTEGER DEFAULT 0,
                    PRIMARY KEY(mode,ticker));
                CREATE INDEX IF NOT EXISTS settlement_due ON settlement_queue(mode,resolved,closes_at);
                CREATE TABLE IF NOT EXISTS dashboard_migrations (name TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS dashboard_settings (name TEXT PRIMARY KEY,payload TEXT);
                """
            )

    def record_candles(self, data, now):
        if not data or not data.get("source"):
            return
        batch = (data.get("source"), data.get("received_at"))
        if batch == self._last_candle_batch:
            return
        with self.connect() as db:
            for bar in data.get("candles", []):
                try:
                    start = int(bar["start"])
                    if start + 60 > min(
                        now.timestamp(), timestamp(data["received_at"]).timestamp()
                    ):
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO btc_candles VALUES (?,?,?,?)",
                        (data["source"], start, json.dumps(bar), data["received_at"]),
                    )
                except (KeyError, ValueError, TypeError):
                    continue

        self._last_candle_batch = batch

    def connect(self):
        return storage_connect(self.path)

    def read(self):
        return storage_connect(self.path, readonly=True)

    def scalp_criteria(self, values=None):
        from .scalp_monitor import ScalpCriteria

        with self.read() if values is None else self.connect() as db:
            if values is not None:
                current = db.execute(
                    "SELECT payload FROM dashboard_settings WHERE name='scalp_criteria'"
                ).fetchone()
                merged = {**(json.loads(current[0]) if current else {}), **values}
                criteria = ScalpCriteria.from_mapping(merged)
                db.execute(
                    "INSERT OR REPLACE INTO dashboard_settings VALUES ('scalp_criteria',?)",
                    (json.dumps(criteria.to_dict(), allow_nan=False),),
                )
                return criteria
            row = db.execute(
                "SELECT payload FROM dashboard_settings WHERE name='scalp_criteria'"
            ).fetchone()
        return ScalpCriteria.from_mapping(json.loads(row[0]) if row else None)

    def step(self, state):
        now = timestamp(state["now"])
        market = state["market"]
        with self.connect() as db:
            prediction_engine.record(db, state, utcnow())
            scalp_paper.record(
                db, state, utcnow() if state["mode"] == "PUBLIC MARKET DATA" else now
            )
            edge_scanner.record(
                db, state, utcnow() if state["mode"] == "PUBLIC MARKET DATA" else now
            )
            pending = db.execute(
                "SELECT * FROM simulations WHERE status IN ('PENDING','AWAITING_ENTRY')"
            ).fetchall()
            for probe in pending:
                last = timestamp(probe["last_at"])
                deadline = timestamp(probe["deadline"])
                same = market and market["ticker"] == probe["ticker"] and not state["stale"]
                bid = None
                if same:
                    side_quote = market.get("quotes", {}).get(probe["side"], {})
                    if side_quote.get("bid_size", 0) >= 1:
                        bid = side_quote.get("bid")
                status, pnl = None, None
                if now < last:
                    status = "UNOBSERVED_CLOCK_RESET"
                elif now == last:
                    continue
                elif same and (
                    market.get("fee_type") not in {"quadratic", "quadratic_with_maker_fees"}
                    or market.get("fee_multiplier") != probe["multiplier"]
                ):
                    status = "UNOBSERVED_FEE_CHANGE"
                elif now > deadline:
                    status = "UNOBSERVED_TIME_LIMIT"
                elif (now - last).total_seconds() > 5 or not same or bid is None:
                    status = "UNOBSERVED_DATA_GAP"
                elif probe["status"] == "AWAITING_ENTRY":
                    if now < timestamp(probe["eligible_at"]):
                        db.execute(
                            "UPDATE simulations SET last_at=? WHERE id=?",
                            (now.isoformat(), probe["id"]),
                        )
                        continue
                    ask = side_quote.get("ask")
                    if ask is None or side_quote.get("ask_size", 0) < 1:
                        status = "UNOBSERVED_ENTRY_DEPTH"
                    elif (
                        ask + probe["slippage"] > probe["entry_cap"] or ask + probe["slippage"] >= 1
                    ):
                        status = "SIMULATED_NO_ENTRY_PRICE_MOVED"
                    elif (timestamp(market["closes_at"]) - now).total_seconds() < probe[
                        "holding_seconds"
                    ]:
                        status = "SIMULATED_NO_ENTRY_TIME"
                    else:
                        entry = ask + probe["slippage"]
                        entry_fee = fee(entry, probe["multiplier"])
                        exit_bid = max(0, bid - probe["slippage"])
                        pnl = exit_bid - entry - entry_fee - fee(exit_bid, probe["multiplier"])
                        stopped = pnl <= -probe["stop"]
                        db.execute(
                            """UPDATE simulations SET entry=?,entry_fee=?,execution_at=?,last_at=?,deadline=?,status=?,
                            exit_at=?,exit_bid=?,exit_fee=?,net_pnl=? WHERE id=?""",
                            (
                                entry,
                                entry_fee,
                                now.isoformat(),
                                now.isoformat(),
                                (now + timedelta(seconds=probe["holding_seconds"])).isoformat(),
                                "SIMULATED_STOP" if stopped else "PENDING",
                                now.isoformat() if stopped else None,
                                exit_bid if stopped else None,
                                fee(exit_bid, probe["multiplier"]) if stopped else None,
                                pnl if stopped else None,
                                probe["id"],
                            ),
                        )
                        continue
                else:
                    bid = max(0, number(bid, 0, 1) - probe["slippage"])
                    pnl = bid - probe["entry"] - probe["entry_fee"] - fee(bid, probe["multiplier"])
                    if now >= deadline:
                        status = "SIMULATED_TIMEOUT"
                    elif pnl <= -probe["stop"]:
                        status = "SIMULATED_STOP"
                    elif pnl >= probe["target"]:
                        status = "SIMULATED_TARGET"
                if status:
                    observed = not status.startswith(("UNOBSERVED", "SIMULATED_NO_ENTRY"))
                    db.execute(
                        """UPDATE simulations SET status=?,exit_at=?,exit_bid=?,exit_fee=?,net_pnl=?
                                  WHERE id=?""",
                        (
                            status,
                            now.isoformat(),
                            bid if observed else None,
                            fee(bid, probe["multiplier"]) if observed else None,
                            pnl if observed else None,
                            probe["id"],
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE simulations SET last_at=?,last_bid=? WHERE id=?",
                        (now.isoformat(), bid, probe["id"]),
                    )
            if not market or not market.get("mapping_verified", True):
                return
            db.execute(
                "INSERT OR IGNORE INTO settlement_queue(mode,ticker,closes_at) VALUES (?,?,?)",
                (state["mode"], market["ticker"], market["closes_at"]),
            )
            e = state["estimate"]
            persisted = state
            coverage = state.get("model", {}).get("coverage", {})
            if coverage:
                summaries = {}
                for source, item in coverage.items():
                    observed = item.get("data", {}).get("observed_at")
                    archived = (
                        observed
                        and db.execute(
                            "SELECT 1 FROM feed_observations WHERE source=? AND timestamp=?",
                            (source, observed),
                        ).fetchone()
                    )
                    summaries[source] = (
                        {
                            "status": item.get("status"),
                            "reason": item.get("reason"),
                            "raw_observation": {"source": source, "timestamp": observed},
                        }
                        if archived
                        else item
                    )
                # Raw books/trade flow are stored once in feed_observations.
                # Existing historical payloads are never rewritten or removed.
                persisted = {**state, "model": {**state["model"], "coverage": summaries}}
            intelligence = state.get("model", {}).get("market_intelligence")
            if intelligence:
                compact_sources = {}
                for source, source_state in intelligence.get("sources", {}).items():
                    compact_sources[source] = {
                        **{k: v for k, v in source_state.items() if k != "windows"},
                        "window_quality": {
                            name: {k: v for k, v in details.items() if k != "features"}
                            for name, details in source_state.get("windows", {}).items()
                        },
                    }
                compact = {
                    k: v
                    for k, v in intelligence.items()
                    if k not in {"sources", "candidate_features", "feature_availability"}
                }
                compact.update(
                    sources=compact_sources,
                    feature_values_location="model.candidate_features.values; captured before outcome",
                )
                persisted = {
                    **persisted,
                    "model": {**persisted["model"], "market_intelligence": compact},
                }
            advanced = state.get("advanced_intelligence")
            if advanced:
                persisted = {
                    **persisted,
                    "advanced_intelligence": {
                        "status": advanced.get("status"),
                        "generated_at": advanced.get("generated_at"),
                        "primary_source": advanced.get("primary_source"),
                        "family_status": {
                            name: {k: family.get(k) for k in ("status", "as_of", "contributing")}
                            for name, family in advanced.get("families", {}).items()
                        },
                        "feature_values_location": "model.candidate_features.values; captured before outcome",
                        "validation": advanced.get("validation"),
                    },
                }
            db.execute(
                """INSERT OR IGNORE INTO predictions
                (mode,ticker,timestamp,direction,scalp,probability_up,probability_down,payload)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    state["mode"],
                    market["ticker"],
                    state["now"],
                    state["direction"],
                    state["scalp"],
                    e["settlement_up"],
                    e["settlement_down"],
                    json.dumps(persisted, allow_nan=False),
                ),
            )
            prediction = db.execute(
                "SELECT id FROM predictions WHERE mode=? AND ticker=? AND timestamp=?",
                (state["mode"], market["ticker"], state["now"]),
            ).fetchone()[0]
            if state["stale"]:
                return
            for side, plan in state["plans"].items():
                # One counterfactual probe per side/minute/settings, not a signal fill.
                recent = db.execute(
                    """SELECT started_at FROM simulations WHERE mode=? AND ticker=?
                    AND side=? AND target=? AND stop=?
                    AND COALESCE(holding_seconds,ROUND((julianday(deadline)-julianday(started_at))*86400))=?
                    AND manual_delay=? AND slippage=?
                    ORDER BY id DESC LIMIT 1""",
                    (
                        state["mode"],
                        market["ticker"],
                        side,
                        plan["net_target"],
                        plan["net_stop"],
                        plan["holding_seconds"],
                        plan.get("manual_delay", 0),
                        plan.get("slippage", 0),
                    ),
                ).fetchone()
                if recent and (now - timestamp(recent[0])).total_seconds() < 60:
                    continue
                if (
                    plan["holding_seconds"] + plan.get("manual_delay", 0)
                    > state["remaining_seconds"]
                    or plan["target_bid"] is None
                ):
                    continue
                quote = market["quotes"][side]
                if quote.get("ask_size", 0) < 1 or quote.get("bid_size", 0) < 1:
                    continue
                if plan.get("model_version"):
                    delay = plan.get("manual_delay", 0)
                    slipped_bid = max(0, quote["bid"] - plan["slippage"])
                    entry = plan["entry"] if delay == 0 else None
                    entry_fee = fee(entry, plan["multiplier"]) if entry is not None else None
                    net = (
                        slipped_bid - entry - entry_fee - fee(slipped_bid, plan["multiplier"])
                        if entry is not None
                        else None
                    )
                    stopped = net is not None and net <= -plan["net_stop"]
                    record = dict(
                        prediction_id=prediction,
                        mode=state["mode"],
                        ticker=market["ticker"],
                        side=side,
                        started_at=now.isoformat(),
                        last_at=now.isoformat(),
                        deadline=(
                            now + timedelta(seconds=delay + plan["holding_seconds"])
                        ).isoformat(),
                        multiplier=plan["multiplier"],
                        target=plan["net_target"],
                        stop=plan["net_stop"],
                        status="AWAITING_ENTRY"
                        if delay
                        else ("SIMULATED_STOP" if stopped else "PENDING"),
                        probability=plan["probability"],
                        eligible_at=(now + timedelta(seconds=delay)).isoformat(),
                        entry_cap=plan["entry"],
                        manual_delay=delay,
                        slippage=plan["slippage"],
                        holding_seconds=plan["holding_seconds"],
                        model_version=plan["model_version"],
                        call_at_prediction=state["scalp"],
                        entry=entry,
                        entry_fee=entry_fee,
                        execution_at=now.isoformat() if not delay else None,
                        exit_at=now.isoformat() if stopped else None,
                        exit_bid=slipped_bid if stopped else None,
                        exit_fee=fee(slipped_bid, plan["multiplier"]) if stopped else None,
                        net_pnl=net if stopped else None,
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO simulations ("
                        + ",".join(record)
                        + ") VALUES ("
                        + ",".join("?" for _ in record)
                        + ")",
                        list(record.values()),
                    )
                    continue
                pnl = (
                    quote["bid"]
                    - plan["entry"]
                    - plan["entry_fee"]
                    - fee(quote["bid"], plan["multiplier"])
                )
                immediate = pnl <= -plan["net_stop"]
                db.execute(
                    """INSERT OR IGNORE INTO simulations
                    (prediction_id,mode,ticker,side,started_at,last_at,deadline,entry,entry_fee,multiplier,
                     target,stop,last_bid,status,probability,exit_at,exit_bid,exit_fee,net_pnl)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        prediction,
                        state["mode"],
                        market["ticker"],
                        side,
                        now.isoformat(),
                        now.isoformat(),
                        (now + timedelta(seconds=plan["holding_seconds"])).isoformat(),
                        plan["entry"],
                        plan["entry_fee"],
                        plan["multiplier"],
                        plan["net_target"],
                        plan["net_stop"],
                        quote["bid"],
                        "SIMULATED_STOP" if immediate else "PENDING",
                        plan["probability"],
                        now.isoformat() if immediate else None,
                        quote["bid"] if immediate else None,
                        fee(quote["bid"], plan["multiplier"]) if immediate else None,
                        pnl if immediate else None,
                    ),
                )

    def settle(self, ticker, yes_result, mode, observed_at=None):
        if yes_result not in {"yes", "no"}:
            return
        origin = "official" if mode == "PUBLIC MARKET DATA" else "replay"
        seen = observed_at or utcnow().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO forecast_outcomes VALUES (?,?,?,?)",
                (ticker, origin, seen, int(yes_result == "yes")),
            )
            db.execute(
                "UPDATE settlement_queue SET resolved=1 WHERE ticker=? AND mode=?", (ticker, mode)
            )
        after = 0
        while True:
            with self.read() as db:
                rows = db.execute(
                    "SELECT id,payload FROM predictions WHERE ticker=? AND mode=? AND id>? "
                    "AND (settlement_up IS NULL OR settlement_origin='legacy_unknown') ORDER BY id LIMIT 50",
                    (ticker, mode, after),
                ).fetchall()
            if not rows:
                break
            # Decode outside the writer lock. Preserve legacy mapping safeguards.
            updates = [
                (int(yes_result == "yes"), origin, seen, row["id"])
                for row in rows
                if json.loads(row["payload"])["market"].get("mapping_verified", True)
            ]
            with self.connect() as db:
                db.executemany(
                    "UPDATE predictions SET settlement_up=?,settlement_origin=?,settled_at=? WHERE id=?",
                    updates,
                )
            after = rows[-1]["id"]

    def record_feeds(self, coverage):
        with self.connect() as db:
            for name, feed in coverage.items():
                data = feed.get("data", {})
                if data.get("observed_at"):
                    db.execute(
                        "INSERT OR IGNORE INTO feed_observations VALUES (?,?,?)",
                        (name, data["observed_at"], json.dumps(feed, allow_nan=False)),
                    )

    def learning_readiness(self):
        with self.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM forecast_outcomes WHERE origin='official'"
            ).fetchone()[0]
        return {
            "official_contracts": count,
            "official_featured_contracts": None,
            "minimum_contracts": 170,
            "reason": f"{count}/170 official outcomes before further feature/chronological eligibility checks. Feature eligibility is checked offline, not by rescanning the full journal each minute. Minimum count is not evidence of skill.",
        }

    def opening_inputs(self, opened):
        with self.read() as db:
            rows = db.execute(
                "SELECT source,payload,first_received_at FROM btc_candles WHERE start+60<=? AND julianday(first_received_at)<=julianday(?) AND source LIKE 'Coinbase%' ORDER BY start DESC LIMIT 1500",
                (int(opened.timestamp()), opened.isoformat()),
            ).fetchall()
        unique = {}
        for row in rows:
            bar = json.loads(row[1])
            unique.setdefault(int(bar["start"]), bar)
        return {
            "candles": list(unique.values()),
            "source": "Coinbase frozen journal history",
            "received_at": max((r[2] for r in rows), default=opened.isoformat()),
        }

    def opening_record(self, ticker, mode, candidate=None, wall_now=None):
        with self.read() if candidate is None else self.connect() as db:
            if candidate is not None:
                prediction_engine.save_opening(db, mode, candidate, wall_now or utcnow())
            return prediction_engine.get_opening(db, ticker, mode)

    def engine_prior(self, ticker, mode):
        with self.read() as db:
            return prediction_engine.prior(db, ticker, mode)

    def recent_states(self):
        with self.read() as db:
            rows = db.execute(
                "SELECT timestamp,json_extract(payload,'$.reference'),json_extract(payload,'$.market'),"
                "json_extract(payload,'$.model.version'),json_extract(payload,'$.estimate') "
                "FROM predictions WHERE mode='PUBLIC MARKET DATA' ORDER BY id DESC LIMIT 600"
            ).fetchall()
        return [
            dict(
                mode="PUBLIC MARKET DATA",
                now=row[0],
                reference=json.loads(row[1]) if row[1] else {},
                market=json.loads(row[2]) if row[2] else None,
                model={"version": row[3]},
                estimate=json.loads(row[4]) if row[4] else {},
            )
            for row in reversed(rows)
        ]

    def performance(self):
        with self.read() as db:
            research = metrics(db)
            engine_metrics = prediction_engine.performance(db, utcnow())
            rows = db.execute(
                """SELECT mode,ticker,direction,probability_up,settlement_up,COALESCE(json_extract(payload,'$.model.version'),'uploaded-scenarios') version
                FROM predictions WHERE direction IN ('UP','DOWN')
                AND probability_up IS NOT NULL AND settlement_up IS NOT NULL
                ORDER BY timestamp,id"""
            ).fetchall()
        unique = {}
        for row in rows:
            unique.setdefault((row["mode"], row["ticker"]), row)
        result = {}
        for mode in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
            samples = [r for (m, _), r in unique.items() if m == mode]
            correct = sum((r["direction"] == "UP") == bool(r["settlement_up"]) for r in samples)
            result[mode] = dict(
                contracts=len(samples),
                correct=correct,
                accuracy_sampling_interval=wilson(correct, len(samples)) if samples else None,
                accuracy=correct / len(samples) if samples else None,
                brier_score=sum((r["probability_up"] - r["settlement_up"]) ** 2 for r in samples)
                / len(samples)
                if samples
                else None,
            )
        by_model = {}
        unique_model = {}
        for row in rows:
            unique_model.setdefault((row["mode"], row["version"], row["ticker"]), row)
        for (mode, version, _), row in unique_model.items():
            group = by_model.setdefault(
                mode + " / " + version, {"contracts": 0, "correct": 0, "brier_score": 0}
            )
            group["contracts"] += 1
            group["correct"] += int((row["direction"] == "UP") == bool(row["settlement_up"]))
            group["brier_score"] += (row["probability_up"] - row["settlement_up"]) ** 2
        for group in by_model.values():
            group["accuracy"] = group["correct"] / group["contracts"]
            group["brier_score"] /= group["contracts"]
            group["accuracy_interval"] = wilson(group["correct"], group["contracts"])
        research["settlement_by_model_and_mode"] = by_model
        return {
            "research": research,
            "prediction_engine": engine_metrics,
            "accuracy_goal": 0.9,
            "validated": False,
            "by_mode": result,
            "method": "First supported directional prediction per settled contract; "
            "UNCLEAR excluded. Replay is synthetic, never live validation. "
            "90% is a measurement goal, not an accuracy claim.",
        }

    def history(self):
        with self.read() as db:
            predictions = [
                dict(row)
                for row in db.execute(
                    "SELECT id,mode,ticker,timestamp,direction,scalp,probability_up,probability_down,settlement_up FROM predictions ORDER BY id DESC LIMIT 200"
                )
            ]
            simulations = [
                dict(row)
                for row in db.execute("SELECT * FROM simulations ORDER BY id DESC LIMIT 200")
            ]
        return {
            "performance": self.performance(),
            "predictions": predictions,
            "simulations": simulations,
            "notice": "COUNTERFACTUAL one-contract probes only — never real fills or trades. "
            "Polling can miss intrapoll barriers; unknown gaps are excluded, not wins.",
        }

    def history_page(self, *, before_id=0, limit=20):
        """Indexed bounded records only. Research scans are a separate opt-in job."""
        limit = max(1, min(int(limit), 100))
        before_id = max(0, int(before_id))
        with self.read() as db:
            rows = db.execute(
                "SELECT id,mode,ticker,timestamp,direction,scalp,probability_up,probability_down,settlement_up "
                "FROM predictions WHERE id<? ORDER BY id DESC LIMIT ?",
                (before_id or 9223372036854775807, limit + 1),
            ).fetchall()
            predictions = [dict(row) for row in rows[:limit]]
            ids = [row["id"] for row in predictions]
            simulations = (
                [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM simulations WHERE prediction_id IN ("
                        + ",".join("?" for _ in ids)
                        + ") ORDER BY id DESC LIMIT ?",
                        (*ids, limit * 2),
                    )
                ]
                if ids
                else []
            )
        return {
            "predictions": predictions,
            "simulations": simulations,
            "pagination": {
                "limit": limit,
                "before_id": before_id,
                "has_more": len(rows) > limit,
                "next_before_id": predictions[-1]["id"] if len(rows) > limit else None,
            },
            "notice": "COUNTERFACTUAL probes are simulated, never actual fills. History loads only on request.",
        }

    def seed_settlement_queue(self):
        """One-time bounded legacy migration in the maintenance worker, never live polling."""
        with self.read() as db:
            done = db.execute(
                "SELECT value FROM dashboard_migrations WHERE name='settlement_queue_v1'"
            ).fetchone()
            if done and done[0] == "done":
                return
            # Only one payload per contract is needed, even with millions of revisions.
            rows = db.execute(
                "SELECT mode,ticker,MIN(id) id FROM predictions GROUP BY mode,ticker"
            ).fetchall()
        for start in range(0, len(rows), 50):
            batch = []
            with self.read() as db:
                for row in rows[start : start + 50]:
                    raw = db.execute(
                        "SELECT payload FROM predictions WHERE id=?", (row["id"],)
                    ).fetchone()
                    try:
                        close = json.loads(raw[0])["market"]["closes_at"]
                        timestamp(close)
                        origin = "official" if row["mode"] == "PUBLIC MARKET DATA" else "replay"
                        resolved = (
                            db.execute(
                                "SELECT 1 FROM forecast_outcomes WHERE ticker=? AND origin=?",
                                (row["ticker"], origin),
                            ).fetchone()
                            is not None
                        )
                        batch.append((row["mode"], row["ticker"], close, int(resolved)))
                    except (KeyError, ValueError, TypeError, AttributeError):
                        continue
            with self.connect() as db:
                db.executemany(
                    "INSERT OR IGNORE INTO settlement_queue(mode,ticker,closes_at,resolved) VALUES (?,?,?,?)",
                    batch,
                )
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO dashboard_migrations VALUES ('settlement_queue_v1','done')"
            )

    def due_settlements(self, now, limit=3):
        with self.read() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT ticker FROM settlement_queue WHERE mode='PUBLIC MARKET DATA' AND resolved=0 "
                    "AND closes_at<=? ORDER BY COALESCE(last_attempt,''),closes_at LIMIT ?",
                    (now.isoformat(), limit),
                )
            ]

    def mark_settlement_attempt(self, ticker, now):
        with self.connect() as db:
            db.execute(
                "UPDATE settlement_queue SET last_attempt=? WHERE mode='PUBLIC MARKET DATA' AND ticker=?",
                (now.isoformat(), ticker),
            )

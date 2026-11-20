"""Exact-minute trade recovery for a candle that both Coinbase candle products omit.

Live incident 2026-11-19: the 21:19 UTC minute was absent from both candle
endpoints, the trade endpoint answered every RFC3339 request with HTTP 500, and
the minute held 2,477 trades (more than one 1,000-trade page). Four openings in a
row (21:30-22:15) were withheld for an incomplete 24h window. These tests pin
the repaired recovery: Unix-second range, newest-first pagination, de-duplication
of the overlapping second, and fail-closed behaviour when completeness cannot be
proven.
"""
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from src.signals_display import monitor as module
from src.signals_display.monitor import PublicFeed
from tests.test_signals_display import NOW

START = int(NOW.timestamp()) // 60 * 60 - 300


def _trade(identifier, second, micro=0, price="100", size="0.01"):
    when = datetime.fromtimestamp(START + second, timezone.utc).replace(microsecond=micro)
    return {"trade_id": str(identifier), "time": when.isoformat(), "price": price, "size": size}


def _endpoint(trades, calls, limit=module.TRADE_RECOVERY_LIMIT):
    """Simulates the verified endpoint: [start, end) in Unix seconds, newest first."""
    ordered = sorted(trades, key=lambda t: t["time"], reverse=True)

    def get(url, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        start, end = int(query["start"][0]), int(query["end"][0])
        calls.append((start, end))
        rows = [
            t for t in ordered
            if start <= datetime.fromisoformat(t["time"]).timestamp() < end
        ][:limit]
        return httpx.Response(200, json={"trades": rows}, request=httpx.Request("GET", url))

    return get


def test_busy_minute_is_recovered_across_pages_without_double_counting(monkeypatch):
    monkeypatch.setattr(module, "TRADE_RECOVERY_LIMIT", 10)
    # 25 trades spread over the minute, several sharing one second so the page
    # boundary second is requested twice and must be de-duplicated.
    trades = [_trade(i, second=(i * 2) % 60, micro=i, price=str(100 + i)) for i in range(25)]
    calls = []
    bar = PublicFeed(_endpoint(trades, calls, limit=10))._candle_from_market_trades(START, START + 60)

    assert bar["trade_count"] == 25
    assert bar["volume"] == pytest.approx(0.25)
    assert bar["high"] == 124.0 and bar["low"] == 100.0
    by_time = sorted(trades, key=lambda t: (t["time"], t["trade_id"]))
    assert bar["open"] == float(by_time[0]["price"])
    assert bar["close"] == float(by_time[-1]["price"])
    assert len(calls) >= 3
    assert calls[0] == (START, START + 60)
    assert all(later[1] < earlier[1] for earlier, later in zip(calls, calls[1:]))


def test_single_page_minute_needs_one_request():
    calls = []
    trades = [_trade(i, second=i) for i in range(5)]
    bar = PublicFeed(_endpoint(trades, calls))._candle_from_market_trades(START, START + 60)
    assert bar["trade_count"] == 5 and calls == [(START, START + 60)]


def test_full_page_inside_one_second_fails_closed(monkeypatch):
    monkeypatch.setattr(module, "TRADE_RECOVERY_LIMIT", 10)
    trades = [_trade(i, second=59, micro=i) for i in range(15)]
    with pytest.raises(ValueError, match="truncated"):
        PublicFeed(_endpoint(trades, [], limit=10))._candle_from_market_trades(START, START + 60)


def test_page_budget_exhausted_fails_closed(monkeypatch):
    monkeypatch.setattr(module, "TRADE_RECOVERY_LIMIT", 2)
    monkeypatch.setattr(module, "TRADE_RECOVERY_MAX_PAGES", 3)
    trades = [_trade(i, second=i) for i in range(40)]
    with pytest.raises(ValueError, match="truncated"):
        PublicFeed(_endpoint(trades, [], limit=2))._candle_from_market_trades(START, START + 60)


def test_conflicting_receipts_for_one_trade_id_fail_closed(monkeypatch):
    monkeypatch.setattr(module, "TRADE_RECOVERY_LIMIT", 2)
    pages = iter([
        [_trade("a", 30, price="101"), _trade("b", 20)],
        [_trade("b", 20, price="999"), _trade("c", 5)],
        [],
    ])

    def get(url, **_kwargs):
        return httpx.Response(200, json={"trades": next(pages)}, request=httpx.Request("GET", url))

    with pytest.raises(ValueError, match="Conflicting"):
        PublicFeed(get)._candle_from_market_trades(START, START + 60)


def test_gap_omitted_by_both_candle_products_is_filled_from_paged_trades(monkeypatch):
    """End to end through candles(): the window becomes READY, so openings are not withheld."""
    monkeypatch.setattr(module, "TRADE_RECOVERY_LIMIT", 10)
    cutoff = int(NOW.timestamp()) // 60 * 60
    absent = START
    trades = [_trade(i, second=(i * 3) % 60, micro=i) for i in range(23)]
    trade_calls = []
    trade_get = _endpoint(trades, trade_calls, limit=10)

    def get(url, **kwargs):
        if url.startswith(module.COINBASE_MARKET_TRADES):
            return trade_get(url, **kwargs)
        query = parse_qs(urlsplit(url).query)
        if "api.exchange.coinbase.com" in url:
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))
        left, right = int(query["start"][0]), int(query["end"][0])
        return httpx.Response(200, json={"candles": [
            dict(start=str(t), open="100", high="102", low="99", close="101", volume="1")
            for t in range(left, right, 60) if t != absent
        ]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(module, "utcnow", lambda: NOW)
    result = PublicFeed(get).candles(NOW)
    assert result["diagnostics"]["status"] == "READY"
    assert len(result["candles"]) == 1440
    assert result["candles"][-1]["start"] == cutoff - 60
    recovered = next(bar for bar in result["candles"] if bar["start"] == absent)
    assert recovered["source"].endswith("exact-minute recovery")
    assert result["diagnostics"]["trade_recovery"] == [
        {"start": absent, "status": "RECOVERED_FROM_VERIFIED_TRADES", "trades": 23}
    ]
    assert len(trade_calls) >= 3

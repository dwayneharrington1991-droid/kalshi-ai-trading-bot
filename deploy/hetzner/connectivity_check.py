#!/usr/bin/env python3
"""Credential-safe production connectivity and STALE-stage diagnostic.

This script performs public reads and, only when server-side monitoring
credentials are configured, the application's fixed authenticated GET and
market-data WebSocket checks. It never prints keys, signatures, headers, raw
account responses, balances, positions, fills, or orders.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import ssl
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HTTP_TARGETS = {
    "kalshi_rest": "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=10",
    "coinbase_spot": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
    "coinbase_candles": "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles",
    "kraken_ticker": "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
    "kraken_candles": "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1",
    "kraken_book": "https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=10",
}


def safe_error(exc: BaseException) -> str:
    text = type(exc).__name__
    reason = getattr(exc, "reason", None)
    if reason is not None:
        text += ":" + type(reason).__name__
    return text


def get_json(url: str, timeout: float = 10) -> tuple[dict, float]:
    started = time.monotonic()
    request = Request(url, headers={"User-Agent": "kalshi-signals-connectivity/1"})
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        if exc.headers.get_content_type() != "application/json":
            raise
        response = exc
    with response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("non-object JSON")
    return value, round((time.monotonic() - started) * 1000, 1)


def rest_checks() -> tuple[dict, str | None]:
    results, ticker = {}, None
    for name, url in HTTP_TARGETS.items():
        try:
            if name == "coinbase_candles":
                end = int(time.time()) // 60 * 60
                url += f"?start={end-600}&end={end}&granularity=ONE_MINUTE&limit=10"
            value, elapsed = get_json(url)
            if name == "kalshi_rest":
                markets = value.get("markets")
                if not isinstance(markets, list):
                    raise ValueError("missing markets list")
                ticker = next(
                    (m.get("ticker") for m in markets if isinstance(m, dict) and m.get("ticker")),
                    None,
                )
                results[name] = {"status": "OK", "latency_ms": elapsed,
                                 "open_markets": len(markets), "active_ticker": ticker}
            else:
                results[name] = {"status": "OK", "latency_ms": elapsed}
        except Exception as exc:
            results[name] = {"status": "FAILED", "error_class": safe_error(exc)}
    return results, ticker


def websocket_check(name: str, url: str, subscription: dict) -> dict:
    try:
        from websockets.sync.client import connect
        started = time.monotonic()
        with connect(url, open_timeout=10, close_timeout=2, max_size=2**20) as ws:
            ws.send(json.dumps(subscription))
            ws.recv(timeout=10)
        return {"status": "OK", "first_message_ms": round((time.monotonic()-started)*1000, 1)}
    except Exception as exc:
        return {"status": "FAILED", "error_class": safe_error(exc)}


def account_checks(ticker: str | None) -> dict:
    from src.signals_display.account_readonly import ReadOnlyKalshi, credential_status

    present = credential_status()
    if not present["KALSHI_MONITOR_KEY_ID"] or not (
        present["KALSHI_MONITOR_PRIVATE_KEY_PEM"] or present["KALSHI_MONITOR_PRIVATE_KEY_PATH"]
    ):
        return {"status": "SKIPPED", "reason": "read-only monitoring credentials not configured"}
    try:
        client = ReadOnlyKalshi.from_environment()
        permission = client.permissions()
        result = {
            "authenticated_get": "OK",
            "read_only_verified": bool(permission.get("read_only_verified")),
            "account_access_blocker": bool(permission.get("account_access_blocker")),
        }
        if ticker and result["read_only_verified"] and not result["account_access_blocker"]:
            from websockets.sync.client import connect
            from src.signals_display.kalshi_market_stream import URL
            started = time.monotonic()
            with connect(URL, additional_headers=client.websocket_headers(), open_timeout=10,
                         close_timeout=2, max_size=2**20) as ws:
                ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                    "channels": ["orderbook_delta"], "market_tickers": [ticker],
                    "use_yes_price": True}}))
                ws.recv(timeout=10)
            result["authenticated_market_websocket"] = "OK"
            result["websocket_first_message_ms"] = round((time.monotonic()-started)*1000, 1)
        else:
            result["authenticated_market_websocket"] = "BLOCKED_OR_NO_ACTIVE_TICKER"
        return result
    except Exception as exc:
        return {"status": "FAILED", "error_class": safe_error(exc)}


def local_dashboard() -> dict:
    output = {}
    for endpoint in ("healthz", "api/startup", "api/state"):
        try:
            value, elapsed = get_json("http://127.0.0.1:8765/" + endpoint, timeout=5)
            if endpoint == "api/state":
                market = value.get("market") or {}
                health = value.get("market_health") or {}
                feeds = value.get("feed_health") or {}
                output[endpoint] = {
                    "status": "OK", "latency_ms": elapsed, "stale": value.get("stale"),
                    "ticker": market.get("ticker"), "now": value.get("now"),
                    "kalshi_connected": feeds.get("kalshi_connection"),
                    "btc_connected": feeds.get("btc_connection"),
                    "quote_reasons": health.get("contract_quote_reasons"),
                    "reference_reasons": health.get("reference_reasons"),
                    "prediction_unavailable_reason": feeds.get("prediction_unavailable_reason"),
                    "latest_reference_at": (value.get("reference") or {}).get("observed_at"),
                    "latest_market_at": market.get("observed_at"),
                    "worker_status": feeds.get("workers"),
                }
            else:
                output[endpoint] = {"status": value.get("status", "OK"),
                                    "stage": value.get("stage"), "latency_ms": elapsed,
                                    "build_commit": value.get("build_commit")}
        except Exception as exc:
            output[endpoint] = {"status": "FAILED", "error_class": safe_error(exc)}
    return output


def main() -> int:
    rest, ticker = rest_checks()
    report = {
        "checked_at_epoch": int(time.time()),
        "proxy_variables_present": sorted(
            key for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
            if os.environ.get(key)
        ),
        "dns_tls": {}, "rest": rest,
        "websockets": {
            "coinbase": websocket_check(
                "coinbase", "wss://advanced-trade-ws.coinbase.com",
                {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "ticker"}),
            "kraken": websocket_check(
                "kraken", "wss://ws.kraken.com/v2",
                {"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD"]}}),
        },
        "kalshi_account_and_ws": account_checks(ticker),
        "dashboard": local_dashboard(),
    }
    for host in ("api.elections.kalshi.com", "api.coinbase.com", "api.kraken.com"):
        try:
            address = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0]
            context = ssl.create_default_context()
            with socket.create_connection((host, 443), timeout=5) as sock:
                with context.wrap_socket(sock, server_hostname=host) as tls:
                    report["dns_tls"][host] = {"status": "OK", "address_family":
                        "IPv6" if ":" in address else "IPv4", "tls": tls.version()}
        except Exception as exc:
            report["dns_tls"][host] = {"status": "FAILED", "error_class": safe_error(exc)}
    stages = [
        *(('dns_tls:' + name, value) for name, value in report["dns_tls"].items()),
        *(('rest:' + name, value) for name, value in rest.items()),
        *(('websocket:' + name, value) for name, value in report["websockets"].items()),
    ]
    report["first_external_failure"] = next(
        ({"stage": name, **value} for name, value in stages if value.get("status") != "OK"),
        None,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    required = list(rest.values()) + list(report["websockets"].values())
    return 0 if all(item.get("status") == "OK" for item in required) else 2


if __name__ == "__main__":
    raise SystemExit(main())

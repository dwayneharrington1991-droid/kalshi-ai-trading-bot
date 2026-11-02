#!/usr/bin/env python3
"""Credential-safe local diagnostics for the signals-only dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
from time import perf_counter, time
import httpx
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


def error_class(error):
    """Credential-safe transport classification without exception messages."""
    if isinstance(error, httpx.ProxyError):
        return "PROXY_CONNECT_FAILED"
    if isinstance(error, httpx.ConnectTimeout):
        return "CONNECT_TIMEOUT"
    if isinstance(error, httpx.ReadTimeout):
        return "READ_TIMEOUT"
    if isinstance(error, httpx.ConnectError):
        return "DNS_TCP_OR_TLS_CONNECT_FAILED"
    name = type(error).__name__
    if "Proxy" in name:
        return "WEBSOCKET_PROXY_REJECTED"
    if "SSL" in name or "TLS" in name:
        return "TLS_HANDSHAKE_FAILED"
    return name


def port_status(port=8765):
    started = perf_counter()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.settimeout(1)
        connected = candidate.connect_ex(("127.0.0.1", port)) == 0
    return {
        "port": port, "listening": connected,
        "connect_ms": round((perf_counter() - started) * 1000, 2),
    }


def git_value(*args):
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5
        ).stdout.strip() or "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def bot_processes(proc_root=Path("/proc")):
    values = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = [p for p in (entry / "cmdline").read_bytes().decode().split("\0") if p]
            kind = None
            port = None
            if "src.signals_display.server" in args:
                kind = "dashboard-server"
                try:
                    port = int(args[args.index("--port") + 1])
                except (ValueError, IndexError):
                    port = 8765
            elif any(p.endswith("scripts/start_dashboard.py") for p in args):
                kind = "dashboard-supervisor"
            if kind:
                status = (entry / "status").read_text()
                rss = next(
                    (line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("VmRSS:")),
                    "unavailable",
                )
                item = {"pid": int(entry.name), "kind": kind, "rss": rss}
                if port is not None:
                    item["port"] = port
                    item["intended_dashboard_port"] = port == 8765
                values.append(item)
        except (OSError, UnicodeError):
            continue
    return sorted(values, key=lambda item: item["pid"])


def health():
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open("http://127.0.0.1:8765/healthz", timeout=3)
    except HTTPError as error:
        response = error
    except Exception as error:
        return {"reachable": False, "error_type": type(error).__name__}
    try:
        return {
            "reachable": True,
            "http_status": response.status,
            "payload": json.load(response),
        }
    except Exception as error:
        return {
            "reachable": True,
            "http_status": getattr(response, "status", None),
            "error_type": type(error).__name__,
        }
    finally:
        response.close()


def database_status(path):
    target = ROOT / path
    result = {"path": path, "exists": target.exists()}
    if not target.exists():
        return result
    result["bytes"] = target.stat().st_size
    try:
        db = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=1)
        try:
            db.execute("PRAGMA query_only=ON")
            result["readable"] = db.execute("SELECT 1").fetchone()[0] == 1
        finally:
            db.close()
    except sqlite3.Error as error:
        result.update(readable=False, error_type=type(error).__name__)
    return result


def capacity():
    disk = os.statvfs(ROOT)
    result = {
        "disk_free_bytes": disk.f_bavail * disk.f_frsize,
        "disk_total_bytes": disk.f_blocks * disk.f_frsize,
    }
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(("MemTotal:", "MemAvailable:")):
                key, value = line.split(":", 1)
                fields[key] = value.strip()
        result.update(fields)
    except OSError:
        pass
    return result


def market_data_connectivity():
    """Fixed public GET checks only; never sends credentials or proxy values."""
    import httpx

    end = int(time()) // 60 * 60
    now_epoch = int(time())
    endpoints = {
        "kalshi_public_markets": (
            "https://api.elections.kalshi.com/trade-api/v2/markets?"
            f"series_ticker=KXBTC15M&min_close_ts={now_epoch}&max_close_ts={now_epoch + 1800}&limit=200"
        ),
        "kalshi_series_metadata": (
            "https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M"
        ),
        "coinbase_btc_spot": "https://api.coinbase.com/v2/prices/BTC-USD/spot",
        "coinbase_btc_candles": (
            "https://api.coinbase.com/api/v3/brokerage/market/products/"
            f"BTC-USD/candles?start={end - 300}&end={end}&granularity=ONE_MINUTE&limit=5"
        ),
    }
    result = {
        "proxy_environment_present": any(
            os.environ.get(name)
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
        ),
        "checks": {},
    }
    with httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False) as client:
        for name, url in endpoints.items():
            started = perf_counter()
            try:
                response = client.get(
                    url, headers={"Accept": "application/json", "Cache-Control": "no-cache"}
                )
                check = {
                    "status": "OK" if response.status_code == 200 else "HTTP_ERROR",
                    "http_status": response.status_code,
                    "content_type": response.headers.get("content-type", "").split(";", 1)[0],
                    "round_trip_ms": round((perf_counter() - started) * 1000, 2),
                }
                if response.status_code == 200:
                    try:
                        payload = response.json()
                        if name == "kalshi_public_markets":
                            rows = payload.get("markets", [])
                            active = [
                                row for row in rows
                                if row.get("status") in {"open", "active"}
                                and str(row.get("ticker", "")).startswith("KXBTC15M-")
                            ]
                            check.update(
                                parsed=True,
                                markets_returned=len(rows),
                                active_or_upcoming_candidates=len(active),
                                tickers=[str(row.get("ticker", ""))[:120] for row in rows[:3]],
                            )
                        elif name == "kalshi_series_metadata":
                            series = payload.get("series", {})
                            check.update(
                                parsed=series.get("ticker") in {None, "KXBTC15M"},
                                settlement_sources_present=bool(series.get("settlement_sources")),
                                fee_metadata_present=series.get("fee_type") is not None,
                            )
                        elif name == "coinbase_btc_spot":
                            data = payload.get("data", {})
                            check.update(
                                parsed=bool(data.get("amount")),
                                pair=f"{data.get('base')}-{data.get('currency')}",
                            )
                        else:
                            candles = payload.get("candles", [])
                            check.update(parsed=isinstance(candles, list), candles_returned=len(candles))
                    except (ValueError, TypeError, AttributeError):
                        check.update(parsed=False, parse_error="INVALID_JSON_SCHEMA")
                result["checks"][name] = check
            except Exception as error:
                result["checks"][name] = {
                    "status": "FAILED",
                    "error_type": type(error).__name__,
                    "failure_class": error_class(error),
                    "round_trip_ms": round((perf_counter() - started) * 1000, 2),
                }
    # Exercise the same public WebSocket used by the dashboard. This sends only
    # a public BTC-USD subscription and never loads account credentials.
    started = perf_counter()
    try:
        import json as json_module
        from websockets.sync.client import connect

        with connect(
            "wss://advanced-trade-ws.coinbase.com",
            open_timeout=5,
            close_timeout=1,
        ) as socket:
            for channel in ("ticker", "heartbeats"):
                socket.send(json_module.dumps({
                    "type": "subscribe", "product_ids": ["BTC-USD"], "channel": channel
                }))
            accepted = None
            heartbeat_received = False
            deadline = perf_counter() + 8
            while perf_counter() < deadline:
                message = json_module.loads(socket.recv(timeout=max(0.1, deadline - perf_counter())))
                if message.get("channel") == "heartbeats":
                    heartbeat_received = True
                if message.get("channel") == "ticker":
                    accepted = "ticker"
                    break
            result["checks"]["coinbase_public_websocket"] = {
                "status": "OK" if accepted else "NO_MARKET_MESSAGE",
                "channel_received": accepted,
                "heartbeat_received": heartbeat_received,
                "round_trip_ms": round((perf_counter() - started) * 1000, 2),
            }
    except Exception as error:
        result["checks"]["coinbase_public_websocket"] = {
            "status": "FAILED",
            "error_type": type(error).__name__,
            "failure_class": error_class(error),
            "round_trip_ms": round((perf_counter() - started) * 1000, 2),
        }
    # Include the running application's own component health, without copying
    # market/account values or any credential material into this report.
    try:
        response = httpx.get(
            "http://127.0.0.1:8765/api/state?view=main",
            timeout=3,
            trust_env=False,
        )
        state = response.json()
        feed = state.get("feed_health", {})
        market = state.get("market") or {}
        preload = state.get("historical_preload", {})
        streaming = state.get("streaming", {})
        kalshi_socket = streaming.get("kalshi", {})
        result["running_pipeline"] = {
            "http_status": response.status_code,
            "build_commit": state.get("runtime", {}).get("build_commit"),
            "mode": state.get("mode"),
            "active_ticker": market.get("ticker"),
            "contract_closes_at": market.get("closes_at"),
            "remaining_seconds": state.get("remaining_seconds"),
            "market_stale": state.get("stale"),
            "last_btc_update": feed.get("last_successful_btc_price_update"),
            "last_kalshi_update": feed.get("last_kalshi_market_update"),
            "rest_status": feed.get("rest_status"),
            "rest_root_failure": feed.get("rest_root_failure"),
            "websocket_status": feed.get("coinbase_websocket_status"),
            "websocket_last_error_type": feed.get("websocket_last_error_type"),
            "kalshi_websocket": kalshi_socket,
            "historical_preload_status": preload.get("status"),
            "historical_candle_count": preload.get("retained_bars"),
            "historical_missing_candles": preload.get("missing_bars"),
            "prediction_engine_status": feed.get("prediction_engine_status"),
            "prediction_unavailable_reason": feed.get("prediction_unavailable_reason"),
            "journal_index_maintenance": feed.get("journal_index_maintenance"),
            "market_data_workers": feed.get("workers"),
            "stale_reason": state.get("explanation"),
            "market_health": state.get("market_health"),
        }
    except Exception as error:
        result["running_pipeline"] = {
            "status": "UNREACHABLE", "error_type": type(error).__name__
        }
    if "running_pipeline" not in result or result["running_pipeline"].get("status") == "UNREACHABLE":
        result["kalshi_websocket_note"] = (
            "Kalshi order-book WebSocket is not anonymous. Its live status can only be read from "
            "a running dashboard after the existing account key passes read-only permission verification; "
            "this diagnostic does not load or transmit credentials. Public REST was tested separately."
        )
    result["notice"] = (
        "Public fixed endpoints only; no credentials, account endpoints, orders or transfers. "
        "Proxy values and response bodies are omitted."
    )
    checks = result["checks"]
    pipeline = result.get("running_pipeline", {})
    stages = (
        ("KALSHI_REST_DISCOVERY", checks.get("kalshi_public_markets", {}).get("status") == "OK", True),
        ("KALSHI_SERIES_METADATA", checks.get("kalshi_series_metadata", {}).get("status") == "OK", True),
        ("BTC_REFERENCE_REST", checks.get("coinbase_btc_spot", {}).get("status") == "OK", True),
        ("BTC_HISTORICAL_CANDLES", checks.get("coinbase_btc_candles", {}).get("status") == "OK", True),
        # WebSocket has a supported REST fallback, so failure is visible but is
        # not the first fatal pipeline stage by itself.
        ("BTC_REFERENCE_WEBSOCKET", checks.get("coinbase_public_websocket", {}).get("status") == "OK", False),
        ("RUNNING_DASHBOARD_PIPELINE", pipeline.get("http_status") == 200, True),
        ("ACTIVE_KXBTC15M_SELECTION", bool(pipeline.get("active_ticker")), True),
        ("FRESH_KALSHI_AND_BTC_INPUTS", pipeline.get("market_stale") is False, True),
    )
    result["pipeline_stages"] = [
        {"stage": name, "status": "PASS" if passed else "FAIL", "required": required}
        for name, passed, required in stages
    ]
    result["first_failing_stage"] = next(
        (name for name, passed, required in stages if required and not passed), None
    )
    return result


def main():
    check_market_data = "--market-data" in sys.argv[1:]
    report = {
        "branch": git_value("branch", "--show-current"),
        "commit": git_value("rev-parse", "HEAD"),
        "credentials_present": {
            # Presence only. Values are never read into the report.
            "key_id": bool(os.environ.get("KALSHI_MONITOR_KEY_ID")),
            "private_key": bool(
                os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PEM")
                or os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PATH")
            ),
        },
        "processes": bot_processes(),
        "port_8765": port_status(),
        "health": health(),
        "capacity": capacity(),
        "databases": [
            database_status("signals_monitor.db"),
            database_status("signals_account_monitor.db"),
        ],
        "security": "Diagnostic performs local reads only; no API or trading request is made.",
    }
    if check_market_data:
        report["market_data_connectivity"] = market_data_connectivity()
        report["security"] = (
            "Diagnostic performs local reads plus fixed unauthenticated public market-data GETs; "
            "no account or trading request is made."
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

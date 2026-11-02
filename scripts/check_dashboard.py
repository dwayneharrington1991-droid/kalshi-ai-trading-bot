#!/usr/bin/env python3
"""Secret-safe diagnostics. Optional authenticated GET-only production check."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def dashboard_state():
    with urlopen("http://127.0.0.1:8765/api/state?view=main", timeout=10) as response:
        return json.load(response)


def safe_summary(state):
    """Bounded, credential-free evidence suitable for terminal diagnostics."""
    market = state.get("market") or {}
    engine = state.get("prediction_engine") or {}
    opening = engine.get("initial") or {}
    health = state.get("feed_health") or {}
    runtime = state.get("runtime") or {}
    advanced = state.get("advanced_intelligence") or {}
    return {
        "observed_at": state.get("now"),
        "published_at": runtime.get("published_at"),
        "state_age_seconds": runtime.get("state_age_seconds"),
        "processing_ms": runtime.get("processing_ms"),
        "update_interval_ms": runtime.get("update_interval_ms"),
        "peak_resident_mib": (runtime.get("resources") or {}).get("peak_resident_mib"),
        "feed_status": health.get("status"),
        "btc_received_at": health.get("last_successful_btc_price_update"),
        "kalshi_received_at": health.get("last_kalshi_market_update"),
        "btc_age_seconds": health.get("btc_age_seconds"),
        "kalshi_age_seconds": health.get("kalshi_age_seconds"),
        "coinbase_websocket": health.get("coinbase_websocket_status"),
        "websocket_reconnects": health.get("websocket_reconnects"),
        "websocket_error_type": health.get("websocket_last_error_type"),
        "rest_status": health.get("rest_status"),
        "ticker": market.get("ticker"),
        "opens_at": market.get("opens_at"),
        "closes_at": market.get("closes_at"),
        "threshold": market.get("strike"),
        "quote_source": market.get("quote_source"),
        "reference_source": (state.get("reference") or {}).get("source"),
        "reference_is_official": (state.get("reference") or {}).get("official"),
        "opening_status": opening.get("status"),
        "opening_generated_at": opening.get("generated_at"),
        "opening_data_cutoff": opening.get("data_cutoff"),
        "opening_reason": opening.get("reason"),
        "opening_probability_up": opening.get("up"),
        "opening_probability_down": opening.get("down"),
        "live_status": engine.get("status"),
        "live_updated_at": engine.get("updated_at"),
        "live_probability_up": engine.get("up"),
        "live_probability_down": engine.get("down"),
        "live_reason": engine.get("reason"),
        "advanced_status": advanced.get("status"),
        "edge_status": (state.get("edge_scanner") or {}).get("label"),
        "scalp_status": (state.get("scalping_monitor") or {}).get("signal_status"),
        "account_status": (state.get("account") or {}).get("status"),
    }


def watch_rollovers(count, interval):
    baseline = None
    completed = 0
    last_printed = None
    while completed < count:
        try:
            state = dashboard_state()
            summary = safe_summary(state)
            ticker = summary["ticker"]
            if baseline is None and ticker:
                baseline = ticker
            elif ticker and baseline and ticker != baseline:
                completed += 1
                baseline = ticker
                summary["rollover_number"] = completed
            signature = (
                ticker,
                summary["feed_status"],
                summary["opening_status"],
                summary["live_updated_at"],
                completed,
            )
            if signature != last_printed:
                print(json.dumps(summary, allow_nan=False), flush=True)
                last_printed = signature
        except Exception as exc:
            print(json.dumps({"dashboard": "UNREACHABLE", "error_type": type(exc).__name__}), flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account-check",
        action="store_true",
        help="Load existing monitor secrets server-side and verify authenticated GET/permissions",
    )
    parser.add_argument(
        "--watch-rollovers",
        type=int,
        default=0,
        choices=range(0, 5),
        metavar="N",
        help="Watch the local dashboard until N contract changes and print secret-safe evidence",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Watcher interval, 0.5 to 10 seconds")
    args = parser.parse_args()
    if not 0.5 <= args.interval <= 10:
        parser.error("--interval must be from 0.5 to 10 seconds")
    result = {
        "credentials": {
            name: bool(os.environ.get(name))
            for name in (
                "KALSHI_MONITOR_KEY_ID",
                "KALSHI_MONITOR_PRIVATE_KEY_PEM",
                "KALSHI_MONITOR_PRIVATE_KEY_PATH",
            )
        }
    }
    try:
        state = dashboard_state()
        result["dashboard"] = state.get(
            "connection_diagnostics",
            {
                "account_status": state.get("account", {}).get("status"),
                "note": "Old dashboard version; update and restart",
            },
        )
    except Exception as exc:
        result["dashboard"] = {"status": "UNREACHABLE", "error_type": type(exc).__name__}
    if args.account_check:
        from src.signals_display.account_readonly import ReadOnlyKalshi, AccountReadError

        try:
            client = ReadOnlyKalshi.from_environment()
            permissions = client.permissions()
            result["production_permission_check"] = permissions
            if permissions["read_only_verified"]:
                client.require_readonly = True
                data = client.snapshot()
                result["production_account_check"] = {
                    "status": "AUTHENTICATED_GET_SUCCEEDED",
                    "positions": len(data["positions"]),
                    "fills": len(data["fills"]),
                    "orders": len(data["orders"]),
                    "balance_present": bool(data["balance"]),
                    "observed_at": data["observed_at"],
                }
            else:
                result["production_account_check"] = {
                    "status": "WITHHELD",
                    "reason": permissions.get("blocker"),
                }
        except AccountReadError as exc:
            result["production_account_check"] = {"status": "WITHHELD", "reason": str(exc)}
        except Exception as exc:
            result["production_account_check"] = {
                "status": "WITHHELD",
                "error_type": type(exc).__name__,
            }
    print(json.dumps(result, indent=2, allow_nan=False))
    if args.watch_rollovers:
        watch_rollovers(args.watch_rollovers, args.interval)


if __name__ == "__main__":
    main()

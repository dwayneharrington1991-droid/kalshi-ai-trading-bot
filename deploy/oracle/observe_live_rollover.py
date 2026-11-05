#!/usr/bin/env python3
"""Bounded read-only Oracle observation across a KXBTC15M rollover."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time
from urllib.request import urlopen


def fetch(path, timeout=5):
    with urlopen("http://127.0.0.1:8765" + path, timeout=timeout) as response:
        return json.load(response)


def summarize(samples, target):
    if not samples:
        raise ValueError("no dashboard observations were collected")
    commits = {item["health"].get("build_commit") for item in samples}
    if commits != {target}:
        raise ValueError("running commit changed or did not match target")
    if any(item["health"].get("signals_only") is not True for item in samples):
        raise ValueError("signals-only health assertion failed")
    if any(not (item["health"].get("workers") or {}).get("healthy") for item in samples):
        raise ValueError("a required worker became unhealthy")
    states = [item["state"] for item in samples]
    tickers = [str((state.get("market") or {}).get("ticker") or "") for state in states]
    tickers = [ticker for ticker in tickers if ticker.startswith("KXBTC15M-")]
    if len(set(tickers)) < 2:
        raise ValueError("no complete KXBTC15M contract rollover was observed")
    reference_times = {
        (state.get("reference") or {}).get("observed_at") for state in states
        if (state.get("reference") or {}).get("observed_at")
    }
    quote_times = {
        (state.get("market") or {}).get("observed_at") for state in states
        if (state.get("market") or {}).get("observed_at")
    }
    if len(reference_times) < 2 or len(quote_times) < 2:
        raise ValueError("market/reference timestamps did not advance")
    chart_starts = {
        item["chart"].get("latest_bar_start") for item in samples
        if item["chart"].get("latest_bar_start") is not None
    }
    if not chart_starts:
        raise ValueError("candlestick chart never produced a verified bar")
    new_ticker = tickers[-1]
    rollover_states = [
        state for state in states if (state.get("market") or {}).get("ticker") == new_ticker
    ]
    opening = [
        (state.get("prediction_engine") or {}).get("initial") or {}
        for state in rollover_states
    ]
    if not any(item.get("generated_at") and item.get("ticker") == new_ticker for item in opening):
        raise ValueError("new contract did not record an immutable opening forecast")
    if any(
        (state.get("decision_intelligence") or {}).get("execution_capability") is not False
        for state in states
    ):
        raise ValueError("decision coordinator execution boundary is not disabled")
    return {
        "status": "PASS",
        "target_commit": target,
        "observations": len(samples),
        "contracts": list(dict.fromkeys(tickers)),
        "reference_timestamps": len(reference_times),
        "quote_timestamps": len(quote_times),
        "chart_latest_intervals": len(chart_starts),
        "opening_forecast_for_new_contract": True,
        "settlement_panel_present": all("prediction_engine" in state for state in states),
        "scalp_panel_present": all("scalping_monitor" in state for state in states),
        "automated_trading": "DISABLED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--duration", type=int, default=1_200)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 900 <= args.duration <= 1_800 or not 2 <= args.interval <= 10:
        parser.error("duration must be 900..1800 and interval 2..10 seconds")
    deadline = time.monotonic() + args.duration
    samples = []
    failures = []
    while time.monotonic() < deadline:
        try:
            samples.append({
                "at": datetime.now(timezone.utc).isoformat(),
                "health": fetch("/healthz"),
                "ready": fetch("/readyz"),
                "state": fetch("/api/state?view=main"),
                "chart": fetch("/api/signal-chart?interval=1m&limit=30"),
            })
        except Exception as exc:
            failures.append(type(exc).__name__)
        time.sleep(args.interval)
    try:
        result = summarize(samples, args.target)
        result["transient_request_failures"] = failures
    except ValueError as exc:
        result = {
            "status": "FAILED",
            "target_commit": args.target,
            "reason": str(exc),
            "observations": len(samples),
            "transient_request_failures": failures,
            "automated_trading": "DISABLED",
        }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()

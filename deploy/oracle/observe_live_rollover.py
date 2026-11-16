#!/usr/bin/env python3
"""Bounded, read-only verification across a KXBTC15M preview rollover."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import time
from urllib.request import ProxyHandler, build_opener


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("required UTC timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks UTC offset")
    return parsed.astimezone(timezone.utc)


def fetch(path, port, timeout=5):
    started = time.perf_counter()
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}" + path, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status} for {path}")
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"non-object response for {path}")
    return value, (time.perf_counter() - started) * 1000


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)


def validate_candle_integrity(value):
    required = {
        "status", "cutoff", "interval_seconds", "starts", "bars", "unique_bars",
        "duplicate_bars", "invalid_bars", "missing_intervals", "unexpected_intervals",
    }
    if not required <= value.keys():
        raise ValueError("candle-integrity response schema is incomplete")
    cutoff, starts = value["cutoff"], value["starts"]
    if not isinstance(cutoff, int) or cutoff % 60:
        raise ValueError("candle-integrity cutoff is not an aligned Unix minute")
    if value["interval_seconds"] != 60 or not isinstance(starts, list):
        raise ValueError("candle-integrity interval/schema is invalid")
    if any(not isinstance(stamp, int) or stamp % 60 for stamp in starts):
        raise ValueError("unaligned candle timestamp")
    expected = list(range(cutoff - 86400, cutoff, 60))
    expected_set, observed = set(expected), set(starts)
    missing = [stamp for stamp in expected if stamp not in observed]
    unexpected = [stamp for stamp in starts if stamp not in expected_set]
    duplicates = len(starts) - len(observed)
    reported_duplicates = value["bars"] - value["unique_bars"]
    if reported_duplicates != value["duplicate_bars"] or reported_duplicates < duplicates:
        raise ValueError("candle duplicate counters disagree")
    duplicates = max(duplicates, reported_duplicates)
    future = [stamp for stamp in starts if stamp + 60 > cutoff]
    if value["missing_intervals"] != missing:
        raise ValueError("reported and calculated missing intervals disagree")
    if value["unexpected_intervals"] != unexpected:
        raise ValueError("reported and calculated unexpected intervals disagree")
    if missing or unexpected or duplicates or future or value["invalid_bars"]:
        labels = [datetime.fromtimestamp(stamp, timezone.utc).isoformat() for stamp in missing]
        raise ValueError(
            "candle history incomplete; WAIT required; missing UTC intervals="
            + (",".join(labels) if labels else "none")
            + f"; duplicates={duplicates}; unexpected={len(unexpected)}; future={len(future)}"
        )
    if starts != expected or value["status"] != "READY":
        raise ValueError("1,440 candles are not unique, ordered and contiguous")
    if value["bars"] != 1440 or value["unique_bars"] != 1440:
        raise ValueError("candle count is not exactly 1440")
    return {"bars": 1440, "first": starts[0], "last": starts[-1], "cutoff": cutoff}


def validate_schema(item, target):
    for name in ("health", "ready", "state", "chart", "integrity"):
        if not isinstance(item.get(name), dict):
            raise ValueError(f"{name} API response is missing")
    health, ready, state, chart = item["health"], item["ready"], item["state"], item["chart"]
    if health.get("status") != "HEALTHY" or health.get("build_commit") != target:
        raise ValueError("health or running commit assertion failed")
    if health.get("signals_only") is not True:
        raise ValueError("signals-only health assertion failed")
    if not (health.get("workers") or {}).get("healthy"):
        raise ValueError("a required worker became unhealthy")
    if ready.get("status") != "READY" or ready.get("signals_only") is not True:
        raise ValueError("/readyz is not READY and signals-only")
    if ready.get("contract_quotes_fresh") is not True or ready.get("reference_fresh") is not True:
        raise ValueError("/readyz reports a stale required feed")
    market = state.get("market") or {}
    if not str(market.get("ticker") or "").startswith("KXBTC15M-"):
        raise ValueError("state has no KXBTC15M contract")
    if ready.get("contract") != market.get("ticker"):
        raise ValueError("/readyz and state contract identities disagree")
    if state.get("stale") is not False:
        raise ValueError("state reports stale market data")
    now = parse_time(state.get("now"))
    opened, closes = parse_time(market.get("opens_at")), parse_time(market.get("closes_at"))
    if not opened <= now < closes or not opened < closes:
        raise ValueError("contract timestamps do not contain the observed state time")
    parse_time(market.get("observed_at"))
    parse_time((state.get("reference") or {}).get("observed_at"))
    if chart.get("status") != "READY" or not chart.get("bars"):
        raise ValueError("one-minute chart is missing")
    if (state.get("decision_intelligence") or {}).get("execution_capability") is not False:
        raise ValueError("decision coordinator execution boundary is not disabled")
    validate_candle_integrity(item["integrity"])


def summarize(samples, target):
    if not samples:
        raise ValueError("no dashboard observations were collected")
    for item in samples:
        validate_schema(item, target)
    states = [item["state"] for item in samples]
    tickers = [(state.get("market") or {}).get("ticker") for state in states]
    contracts = []
    for ticker in tickers:
        if not contracts or contracts[-1] != ticker:
            contracts.append(ticker)
    if len(contracts) < 2:
        raise ValueError("no complete KXBTC15M contract rollover was observed")
    if any(ticker in contracts[index + 1:] for index, ticker in enumerate(contracts[:-1])):
        raise ValueError("contract identity moved backward after rollover")
    transition = next(index for index in range(1, len(tickers)) if tickers[index] != tickers[index - 1])
    old_market, new_market = states[transition - 1]["market"], states[transition]["market"]
    if parse_time(new_market["opens_at"]) < parse_time(old_market["opens_at"]):
        raise ValueError("contract opening timestamp moved backward")
    if parse_time(new_market["closes_at"]) <= parse_time(old_market["closes_at"]):
        raise ValueError("contract closing timestamp did not advance")
    if parse_time(new_market["opens_at"]) != parse_time(old_market["closes_at"]):
        raise ValueError("contract identifiers changed without a contiguous 15-minute boundary")
    if (parse_time(new_market["closes_at"]) - parse_time(new_market["opens_at"])).total_seconds() != 900:
        raise ValueError("new contract is not a 15-minute interval")
    chart_starts = [item["chart"].get("latest_bar_start") for item in samples]
    if any(not isinstance(value, int) or value % 60 for value in chart_starts):
        raise ValueError("latest closed candle timestamp is missing or unaligned")
    if any(right < left for left, right in zip(chart_starts, chart_starts[1:])):
        raise ValueError("closed candle timeline moved backward")
    completed_rollovers = sum(right == left + 60 for left, right in zip(chart_starts, chart_starts[1:]))
    if completed_rollovers < 2:
        raise ValueError("fewer than two completed one-minute candle rollovers were observed")
    new_ticker = contracts[-1]
    rollover_states = [state for state in states if (state.get("market") or {}).get("ticker") == new_ticker]
    openings = [(state.get("prediction_engine") or {}).get("initial") or {} for state in rollover_states]
    recorded = [item for item in openings if item.get("ticker") == new_ticker]
    if len(recorded) < 2:
        raise ValueError("new contract did not expose a persistent opening forecast")
    immutable_fields = ("ticker", "generated_at", "data_cutoff", "input_data_cutoff", "direction", "up", "down", "status")
    if len({tuple(item.get(field) for field in immutable_fields) for item in recorded}) != 1:
        raise ValueError("new contract opening forecast changed after it was recorded")
    opening_time = parse_time(rollover_states[0]["market"]["opens_at"])
    if parse_time(recorded[0].get("data_cutoff")) != opening_time:
        raise ValueError("opening contractual cutoff does not equal contract opening")
    if not parse_time(recorded[0].get("input_data_cutoff")) < opening_time:
        raise ValueError("opening input cutoff is not strictly before contract opening")
    if recorded[0].get("direction") not in {"UP", "DOWN", "WAIT", "UNCLEAR"}:
        raise ValueError("opening direction schema is invalid")
    up, down = recorded[0].get("up"), recorded[0].get("down")
    if up is not None or down is not None:
        if not all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in (up, down)):
            raise ValueError("opening probabilities are invalid")
        if abs(up + down - 1) > 1e-9:
            raise ValueError("opening probabilities do not sum to one")
    live_updates = {((state.get("prediction_engine") or {}).get("updated_at"), (state.get("prediction_engine") or {}).get("direction")) for state in rollover_states}
    scalp_updates = {((state.get("scalping_monitor") or {}).get("timestamp"), state.get("scalp")) for state in rollover_states}
    if len(live_updates) < 2:
        raise ValueError("LIVE prediction did not update independently")
    if len(scalp_updates) < 2:
        raise ValueError("SCALP analysis did not update independently")
    storage = [state.get("storage_health") or {} for state in states]
    if any(item.get("status") == "CRITICAL_DISK_PRESSURE" for item in storage):
        raise ValueError("critical disk pressure observed")
    if any((item.get("filesystem_free_bytes") or 0) < 10 * 1024**3 for item in storage):
        raise ValueError("less than 10 GiB free storage observed")
    request_ms = [value for item in samples for value in item.get("request_latency_ms", [])]
    reference_age_ms, quote_age_ms = [], []
    for item in samples:
        observed, state = parse_time(item["at"]), item["state"]
        reference_age_ms.append(max(0, (observed - parse_time(state["reference"]["observed_at"])).total_seconds() * 1000))
        quote_age_ms.append(max(0, (observed - parse_time(state["market"]["observed_at"])).total_seconds() * 1000))
    return {
        "status": "PASS", "target_commit": target, "observations": len(samples),
        "contracts": contracts, "completed_candle_rollovers": completed_rollovers,
        "candle_integrity": validate_candle_integrity(samples[-1]["integrity"]),
        "opening_forecast_for_new_contract": True, "opening_forecast_immutable": True,
        "opening_cutoff_strictly_preopen": True, "live_updates": len(live_updates),
        "scalp_updates": len(scalp_updates),
        "latency": {"http_p50_ms": percentile(request_ms, .50), "http_p95_ms": percentile(request_ms, .95), "reference_receipt_to_observer_p95_ms": percentile(reference_age_ms, .95), "quote_receipt_to_observer_p95_ms": percentile(quote_age_ms, .95), "scope": "Observer HTTP plus receipt-to-observer age; not one-way exchange latency"},
        "storage_last": storage[-1], "automated_trading": "DISABLED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--duration", type=int, default=1_200)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not 900 <= args.duration <= 1_800 or not 2 <= args.interval <= 10:
        parser.error("duration must be 900..1800 and interval 2..10 seconds")
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    deadline = time.monotonic() + args.duration
    samples, failures = [], []
    while time.monotonic() < deadline:
        try:
            at = datetime.now(timezone.utc).isoformat()
            health, h_ms = fetch("/healthz", args.port)
            ready, r_ms = fetch("/readyz", args.port)
            state, s_ms = fetch("/api/state?view=main", args.port)
            chart, c_ms = fetch("/api/signal-chart?interval=1m&limit=500", args.port)
            integrity, i_ms = fetch("/api/candle-integrity", args.port)
            samples.append({"at": at, "health": health, "ready": ready, "state": state, "chart": chart, "integrity": integrity, "request_latency_ms": [h_ms, r_ms, s_ms, c_ms, i_ms]})
        except Exception as exc:
            failures.append(type(exc).__name__)
        time.sleep(args.interval)
    try:
        result = summarize(samples, args.target)
        if failures:
            raise ValueError(f"unexpected API failures: {failures}")
        result["transient_request_failures"] = []
    except ValueError as exc:
        result = {"status": "FAILED", "target_commit": args.target, "reason": str(exc), "observations": len(samples), "transient_request_failures": failures, "automated_trading": "DISABLED"}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()

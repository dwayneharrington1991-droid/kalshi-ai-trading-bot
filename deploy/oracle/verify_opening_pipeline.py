#!/usr/bin/env python3
"""Require a fresh live forecast and one immutable opening forecast."""
import argparse
from datetime import datetime, timezone
import json
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener


def read_state():
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:8765/api/state?view=main", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"state endpoint returned HTTP {response.status}")
        return json.load(response)


def validate(state):
    engine = state.get("prediction_engine") or {}
    opening = engine.get("initial") or {}
    market = state.get("market") or {}
    if state.get("stale") is True:
        return None, "live market state is stale"
    ticker = engine.get("ticker") or market.get("ticker")
    if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
        return None, "active KXBTC15M contract unavailable"
    if not isinstance(state.get("remaining_seconds"), (int, float)) or state["remaining_seconds"] <= 0:
        return None, "contract countdown is unavailable"
    if not isinstance(engine.get("up"), (int, float)) or not 0 <= engine["up"] <= 1:
        return None, "fresh live probability unavailable"
    if not isinstance(opening.get("up"), (int, float)) or not 0 <= opening["up"] <= 1:
        return None, "opening unavailable: " + str(opening.get("reason") or opening.get("status"))
    try:
        opened = datetime.fromisoformat(str(opening["opens_at"]).replace("Z", "+00:00"))
        cutoff = datetime.fromisoformat(str(opening["data_cutoff"]).replace("Z", "+00:00"))
        generated = datetime.fromisoformat(str(opening["generated_at"]).replace("Z", "+00:00"))
        closed = datetime.fromisoformat(str(opening["closes_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"opening chronology unavailable: {exc}"
    if opened.tzinfo is None or any(value.tzinfo is None for value in (cutoff, generated, closed)):
        return None, "opening chronology is not timezone-aware"
    if cutoff != opened or not opened <= generated < closed:
        return None, "opening chronology or cutoff is invalid"
    if opening.get("ticker") != ticker:
        return None, "opening ticker does not match active contract"
    return {
        "ticker": ticker,
        "up": opening["up"],
        "generated_at": opening["generated_at"],
        "data_cutoff": opening["data_cutoff"],
        "model": opening.get("model"),
    }, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-seconds", type=int, default=1100)
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args()
    if not 10 <= args.max_seconds <= 1800 or not 0.1 <= args.interval <= 30:
        parser.error("unsafe verification bounds")
    deadline = monotonic() + args.max_seconds
    last_reason = "no state received"
    candidate = None
    while monotonic() < deadline:
        try:
            state = read_state()
            value, reason = validate(state)
            if value is None:
                last_reason = reason
            elif candidate is None or candidate["ticker"] != value["ticker"]:
                candidate = value
                last_reason = "opening captured; checking immutable reread"
            elif candidate == value:
                print(json.dumps({
                    "opening_pipeline": "VERIFIED",
                    "immutable_reread": True,
                    **value,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                }, sort_keys=True))
                return 0
            else:
                raise RuntimeError("opening forecast changed after persistence")
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        sleep(args.interval)
    raise SystemExit("opening pipeline verification timed out: " + last_reason)


if __name__ == "__main__":
    raise SystemExit(main())

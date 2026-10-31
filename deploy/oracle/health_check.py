#!/usr/bin/env python3
"""Verify application identity, workers, freshness plumbing, and loopback binding."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def read_json(path):
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open("http://127.0.0.1:8765" + path, timeout=5)
    except HTTPError as exc:
        response = exc
    except URLError as exc:
        raise SystemExit("dashboard endpoint is not accepting connections yet") from exc
    with response:
        return response.status, json.load(response)


def validate_live_workers(health):
    """Use the live health payload, never the prior snapshot embedded in state.

    The state is assembled inside a prediction cycle before the collector's
    post-cycle counter is incremented. Rechecking that embedded counter can
    reject a collector that `/healthz` has already proved alive and current.
    """
    workers = ((health.get("workers") or {}).get("workers") or {})
    for name in ("collector", "prediction"):
        item = workers.get(name) or {}
        if not item.get("thread_alive"):
            raise ValueError(name + " worker thread is not alive")
    if not isinstance(health.get("state_version"), int) or health["state_version"] < 1:
        raise ValueError("prediction state has not completed")
    return workers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--require-models", choices=("0", "1"), default="0")
    args = parser.parse_args()
    status, health = read_json("/healthz")
    if status != 200 or health.get("status") != "HEALTHY":
        raise SystemExit("health endpoint is not HEALTHY")
    if health.get("build_commit") != args.commit:
        raise SystemExit("running commit does not match tested release")
    try:
        workers = validate_live_workers(health)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _, state = read_json("/api/state?view=main")
    listeners = subprocess.run(
        ["ss", "-ltn"], check=True, capture_output=True, text=True, timeout=5
    ).stdout
    if "127.0.0.1:8765" not in listeners or "0.0.0.0:8765" in listeners:
        raise SystemExit("dashboard is not exclusively bound to loopback")
    required = [args.data_root / "signals_monitor.db"]
    if args.require_models == "1":
        required += [args.data_root / "signals_model.json",
                     args.data_root / "signals_opening_model.json"]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise SystemExit("required persistent files missing: " + ",".join(missing))
    print(json.dumps({
        "status": "HEALTHY", "commit": args.commit,
        "state_version": health.get("state_version"),
        "mode": state.get("mode"), "market_stale": state.get("stale"),
        "workers": {name: workers[name].get("cycles") for name in ("collector", "prediction")},
        "signals_only": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()


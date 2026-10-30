#!/usr/bin/env python3
"""Verify application identity, workers, freshness plumbing, and loopback binding."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def read_json(path):
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open("http://127.0.0.1:8765" + path, timeout=5)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


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
    _, state = read_json("/api/state?view=main")
    workers = (state.get("feed_health") or {}).get("workers") or {}
    for name in ("collector", "prediction"):
        item = workers.get(name) or {}
        if not item.get("thread_alive") or item.get("cycles", 0) < 1:
            raise SystemExit(name + " worker is not running")
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


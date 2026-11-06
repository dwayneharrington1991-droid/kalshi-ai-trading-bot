#!/usr/bin/env python3
"""One bounded, read-only production audit for the signals dashboard.

No credential values, raw account records, or prediction histories are emitted.
SQLite is opened query-only and every external command is informational.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
import json
from pathlib import Path
import sqlite3
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def endpoint(path: str, port: int = 8765):
    opener = build_opener(ProxyHandler({}))
    try:
        response = opener.open(f"http://127.0.0.1:{port}{path}", timeout=8)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, json.load(response)


def systemd(unit: str):
    properties = "LoadState,ActiveState,SubState,UnitFileState,MainPID,NRestarts,ExecMainStatus"
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=" + properties],
        capture_output=True, text=True, timeout=8, check=False,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    values["query_return_code"] = result.returncode
    return values


def load_json(path: Path | None):
    if path is None or not path.is_file() or path.stat().st_size > 2_000_000:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"status": "INVALID_JSON", "path": str(path)}


def model_summary(report):
    if not isinstance(report, dict):
        return {"status": "REPORT_UNAVAILABLE"}
    test = report.get("test") or {}
    selected = test.get("selected_candidate") if isinstance(test, dict) else None
    metrics = selected if isinstance(selected, dict) else test if isinstance(test, dict) else {}
    return {
        "status": report.get("status"),
        "eligible_contracts": report.get(
            "available_eligible_contracts", report.get("opening_contracts", report.get("contracts"))
        ),
        "unseen_forward_contracts": report.get("unseen_forward_contracts"),
        "required_unseen_forward_contracts": report.get("required_unseen_forward_contracts", 30),
        "additional_unseen_contracts_needed": report.get("additional_unseen_contracts_needed"),
        "accuracy": metrics.get("accuracy", metrics.get("directional_accuracy")),
        "brier": metrics.get("brier", metrics.get("brier_score")),
        "log_loss": metrics.get("log_loss"),
        "calibration": report.get("expected_calibration_error", metrics.get("expected_calibration_error")),
        "after_cost_test": report.get("after_cost_test"),
        "trained": report.get("trained"),
        "promotion_allowed": report.get("promotion_allowed", False),
    }


def database(path: Path):
    uri = path.resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=5000")
        indexes = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        counts = {
            "recorded_contracts": db.execute(
                "SELECT COUNT(*) FROM btc_engine_contracts WHERE mode='PUBLIC MARKET DATA'"
            ).fetchone()[0],
            "official_outcomes": db.execute(
                "SELECT COUNT(*) FROM forecast_outcomes WHERE origin='official'"
            ).fetchone()[0],
            "recorded_candles": db.execute("SELECT COUNT(*) FROM btc_candles").fetchone()[0],
            "opening_records": db.execute(
                "SELECT COUNT(*) FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA'"
            ).fetchone()[0],
        }
        required = {
            "predictions_ticker_mode", "btc_candles_start", "settlement_due",
            "btc_engine_contracts_close", "scalp_book_ticker_time",
        }
        return {
            "path": str(path), "bytes": path.stat().st_size, "counts": counts,
            "required_indexes_present": sorted(required & indexes),
            "required_indexes_missing": sorted(required - indexes),
            "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0],
            "query_only": bool(db.execute("PRAGMA query_only").fetchone()[0]),
        }
    finally:
        db.close()


def latest_matching(root: Path, suffix: str):
    paths = [Path(item) for item in glob.glob(str(root / ("*" + suffix)))]
    return max(paths, key=lambda item: item.stat().st_mtime, default=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy-root", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    current = args.deploy_root / "current"
    release = current.resolve(strict=True)
    commit = (release / ".deployed-commit").read_text().strip()
    health_code, health = endpoint("/healthz", args.port)
    ready_code, ready = endpoint("/readyz", args.port)
    _, state = endpoint("/api/state?view=main", args.port)
    charts = {}
    for interval in ("15s", "1m", "3m", "5m", "15m"):
        code, value = endpoint(
            f"/api/signal-chart?interval={interval}&limit=30", args.port
        )
        charts[interval] = {
            "http_status": code, "status": value.get("status"),
            "bars": len(value.get("bars") or []),
            "forming_candle": bool(value.get("forming_candle")),
            "reason": value.get("reason"),
        }

    evaluation_root = args.deploy_root / "evaluations"
    latest_dirs = sorted(
        (item for item in evaluation_root.glob("*") if item.is_dir()),
        key=lambda item: item.stat().st_mtime, reverse=True,
    )
    evaluation = latest_dirs[0] if latest_dirs else None
    models = {
        name: model_summary(load_json(evaluation / f"{name}.report.json") if evaluation else None)
        for name in ("opening", "settlement", "scalp")
    }
    learning = load_json(args.deploy_root / "learning-status" / "latest.json")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production": {
            "commit": commit, "release": str(release),
            "health_http": health_code, "health": health,
            "ready_http": ready_code, "ready": ready,
        },
        "systemd": {
            unit: systemd(unit) for unit in (
                "kalshi-signals.service", "kalshi-learning.service", "kalshi-learning.timer"
            )
        },
        "feeds": state.get("feed_health"),
        "market": {
            "ticker": (state.get("market") or {}).get("ticker"),
            "remaining_seconds": state.get("remaining_seconds"),
            "stale": state.get("stale"),
            "opening_status": (state.get("prediction_engine") or {}).get("initial", {}).get("status"),
            "live_status": (state.get("prediction_engine") or {}).get("status"),
            "scalp": state.get("scalp"),
        },
        "runtime": state.get("runtime"),
        "charts": charts,
        "background_learning": learning or state.get("background_learning"),
        "latest_evaluation": str(evaluation) if evaluation else None,
        "models": models,
        "database": database(args.journal),
        "safety": {
            "signals_only": health.get("signals_only") is True,
            "execution_capability": (state.get("decision_intelligence") or {}).get("execution_capability"),
            "model_promotion_performed": False,
            "automated_trading": "DISABLED",
        },
    }
    critical = []
    if health_code != 200 or health.get("status") != "HEALTHY":
        critical.append("dashboard_health")
    if health.get("build_commit") != commit:
        critical.append("commit_identity")
    if health.get("signals_only") is not True:
        critical.append("signals_only")
    if not (health.get("workers") or {}).get("healthy"):
        critical.append("workers")
    if charts["1m"]["status"] != "READY" or not charts["1m"]["bars"]:
        critical.append("one_minute_chart")
    if report["database"]["required_indexes_missing"]:
        critical.append("database_indexes")
    report["critical_failures"] = critical
    print(json.dumps(report, sort_keys=True, indent=2))
    raise SystemExit(1 if critical else 0)


if __name__ == "__main__":
    main()

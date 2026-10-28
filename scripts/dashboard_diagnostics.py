#!/usr/bin/env python3
"""Credential-safe local diagnostics for the signals-only dashboard."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]


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
            if "src.signals_display.server" in args:
                kind = "dashboard-server"
            elif any(p.endswith("scripts/start_dashboard.py") for p in args):
                kind = "dashboard-supervisor"
            if kind:
                status = (entry / "status").read_text()
                rss = next(
                    (line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("VmRSS:")),
                    "unavailable",
                )
                values.append({"pid": int(entry.name), "kind": kind, "rss": rss})
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


def main():
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
        "health": health(),
        "capacity": capacity(),
        "databases": [
            database_status("signals_monitor.db"),
            database_status("signals_account_monitor.db"),
        ],
        "security": "Diagnostic performs local reads only; no API or trading request is made.",
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

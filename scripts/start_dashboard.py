#!/usr/bin/env python3
"""One-command signals dashboard. Never imports an execution client."""
import argparse
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Stop only dashboard servers from this checkout on port 8765",
    )
    parser.add_argument(
        "--private-dashboard",
        action="store_true",
        help="Confirm port 8765 is Private in Codespaces Ports",
    )
    parser.add_argument("--public-only", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    lock = open(ROOT / ".dashboard-launch.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "Another dashboard launcher is running.\n")
    matches = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            parts = (proc / "cmdline").read_bytes().decode().split("\0")
            if (proc / "cwd").resolve() != ROOT:
                continue
            if "-m" not in parts or parts[parts.index("-m") + 1] != "src.signals_display.server":
                continue
            port = parts[parts.index("--port") + 1] if "--port" in parts else "8765"
            if port == "8765":
                matches.append(int(proc.name))
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
    print(f"Verified dashboard servers on port 8765 in this checkout: {len(matches)}", flush=True)
    if matches and not args.restart:
        parser.exit(1, "Dashboard already running. Use --restart or open Ports > 8765.\n")
    for pid in matches:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 15

    def running(pid):
        try:
            return Path("/proc", str(pid), "stat").read_text().split(") ", 1)[1][0] != "Z"
        except OSError:
            return False

    while any(running(pid) for pid in matches):
        if time.monotonic() > deadline:
            parser.exit(
                1,
                "Old dashboard has not stopped. No database was deleted; stop its terminal with Ctrl+C.\n",
            )
        time.sleep(0.1)
    python = ROOT / ".venv/bin/python3"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], check=True)
    check = subprocess.run(
        [
            str(python),
            "-c",
            "import httpx,cryptography; from websockets.sync.client import connect; from importlib.metadata import version; assert int(version('websockets').split('.')[0]) >= 16",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode:
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "httpx==0.27.0",
                "websockets==16.1.1",
                "cryptography==42.0.0",
            ],
            check=True,
        )
    configured = bool(
        os.environ.get("KALSHI_MONITOR_KEY_ID")
        and (
            os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PEM")
            or os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PATH")
        )
    )
    account = configured and not args.public_only
    if account:
        private = False
        if os.environ.get("CODESPACE_NAME"):
            try:
                result = subprocess.run(
                    [
                        "gh",
                        "codespace",
                        "ports",
                        "visibility",
                        "8765:private",
                        "-c",
                        os.environ["CODESPACE_NAME"],
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                )
                private = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        if not private and not args.private_dashboard:
            parser.exit(
                1,
                "Set Ports > 8765 > Port Visibility > Private, then rerun with --private-dashboard.\n",
            )
        if not private:
            print(
                "Port privacy not API-verified: relying on your --private-dashboard confirmation.",
                flush=True,
            )
    command = [
        str(python),
        "-m",
        "src.signals_display.server",
        "--mode",
        "public",
        "--host",
        "0.0.0.0",
        "--port",
        "8765",
        "--journal",
        "signals_monitor.db",
        "--stop",
        "0.10",
    ]
    if account:
        command += [
            "--account-readonly",
            "--private-dashboard",
            "--require-readonly-scope",
            "--account-store",
            "signals_account_monitor.db",
        ]
    print(
        "Starting "
        + (
            "authenticated monitoring; broker read-only permissions must verify"
            if account
            else "public data only; account not configured/enabled"
        )
        + ". Journals preserved.",
        flush=True,
    )
    print("Open Codespaces Ports > 8765 > Open in Browser. Keep visibility Private.", flush=True)
    child = subprocess.Popen(command, cwd=ROOT)
    fcntl.flock(lock, fcntl.LOCK_UN)
    lock.close()
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT)
        return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())

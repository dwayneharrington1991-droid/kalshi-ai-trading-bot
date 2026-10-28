#!/usr/bin/env python3
"""Safe fast-forward update followed by the supervised signals-only launcher."""
from datetime import datetime, timezone
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BRANCH = "codex/kxbtc15m-signals-display"


def run(command, *, capture=False):
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
        timeout=120,
    )


def output(command):
    return run(command, capture=True).stdout.strip()


def update_checkout():
    """Preserve dirty tracked work, then accept only a remote fast-forward."""
    branch = output(["git", "branch", "--show-current"])
    if branch != BRANCH:
        raise RuntimeError(
            f"This Codespace is on '{branch or 'detached HEAD'}', not '{BRANCH}'. "
            "No checkout or files were changed."
        )
    saved = None
    if output(["git", "status", "--porcelain"]):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        saved = f"dashboard-auto-preserve-{stamp}"
        # Untracked source/configuration is preserved. Ignored journals, models,
        # credentials and runtime files remain untouched in place.
        run(["git", "stash", "push", "--include-untracked", "-m", saved])
        print(f"Preserved local source changes in Git stash: {saved}", flush=True)
    run(["git", "fetch", "origin", BRANCH])
    try:
        run(["git", "merge", "--ff-only", f"origin/{BRANCH}"])
    except subprocess.CalledProcessError as exc:
        note = f" Local source changes remain preserved as '{saved}'." if saved else ""
        raise RuntimeError(
            "The branch cannot be fast-forwarded safely. No reset was performed." + note
        ) from exc
    commit = output(["git", "rev-parse", "--short=7", "HEAD"])
    print(f"Dashboard code is current at build {commit}.", flush=True)
    return commit, saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    try:
        update_checkout()
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        parser.exit(
            1,
            "Safe dashboard update stopped: " + str(exc) + "\n"
            "Nothing was reset or deleted. Open the task terminal and share this exact message.\n",
        )
    launcher = ROOT / "scripts/start_dashboard.py"
    values = [sys.executable, str(launcher), "--private-dashboard", "--open-browser"]
    if args.restart:
        values.append("--restart")
    os.execv(sys.executable, values)


if __name__ == "__main__":
    main()

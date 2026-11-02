#!/usr/bin/env python3
"""Create one bounded and verified SQLite online backup."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
from time import monotonic


class BackupLimitExceeded(RuntimeError):
    """The source could not produce a bounded consistent snapshot."""


class ProgressGuard:
    def __init__(self, *, database, max_seconds, stall_seconds, max_restarts):
        self.database = database
        self.deadline = monotonic() + max_seconds
        self.stall_seconds = stall_seconds
        self.max_restarts = max_restarts
        self.last_advance = monotonic()
        self.last_report = 0.0
        self.high_water = 0
        self.previous_copied = 0
        self.restarts = 0

    def __call__(self, status, remaining, total):
        now = monotonic()
        copied = max(0, total - remaining)
        if copied < self.previous_copied:
            self.restarts += 1
            print(
                f"backup_restart database={self.database} count={self.restarts} "
                f"previous_pages={self.previous_copied} current_pages={copied}", flush=True
            )
        if copied > self.high_water:
            self.high_water = copied
            self.last_advance = now
        self.previous_copied = copied
        if remaining == 0 or now - self.last_report >= 5:
            percent = round(copied * 100 / total, 1) if total else 100.0
            print(
                f"backup_progress database={self.database} pages={copied}/{total} "
                f"percent={percent} restarts={self.restarts} status={status}", flush=True
            )
            self.last_report = now
        if self.restarts > self.max_restarts:
            raise BackupLimitExceeded(
                f"source changed too often: restarts={self.restarts} limit={self.max_restarts}"
            )
        if now > self.deadline:
            raise BackupLimitExceeded("backup wall-clock deadline exceeded")
        if remaining and now - self.last_advance > self.stall_seconds:
            raise BackupLimitExceeded("backup made no forward progress before stall deadline")


def backup_database(source, destination, *, max_seconds=900, stall_seconds=120,
                    max_restarts=3, busy_timeout_ms=30_000, pages=4096):
    source, destination = source.resolve(), destination.resolve()
    if not source.is_file() or destination.exists():
        raise ValueError("source must exist and destination must not exist")
    if max_seconds <= 0 or stall_seconds <= 0 or max_restarts < 0 or busy_timeout_ms < 0 or pages <= 0:
        raise ValueError("backup limits must be positive and max-restarts cannot be negative")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.partial.{os.getpid()}")
    if partial.exists():
        raise FileExistsError(f"partial destination already exists: {partial}")
    print(
        f"backup_open source={source.name} bytes={source.stat().st_size} "
        f"max_seconds={max_seconds:g} stall_seconds={stall_seconds:g} "
        f"max_restarts={max_restarts} busy_timeout_ms={busy_timeout_ms}", flush=True
    )
    guard = ProgressGuard(database=source.name, max_seconds=max_seconds,
                          stall_seconds=stall_seconds, max_restarts=max_restarts)
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True,
                             timeout=busy_timeout_ms / 1000) as src:
            src.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            with sqlite3.connect(partial, timeout=busy_timeout_ms / 1000) as dst:
                dst.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
                src.backup(dst, pages=pages, progress=guard, sleep=0.05)
                print(f"integrity_check_started database={destination.name}", flush=True)
                result = dst.execute("PRAGMA quick_check").fetchone()[0]
                if result != "ok":
                    raise RuntimeError(f"backup integrity check failed: {result}")
                print(f"integrity_check_ok database={destination.name}", flush=True)
        with partial.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(partial, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    digest = hashlib.sha256()
    print(f"checksum_started database={destination.name}", flush=True)
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        digest.hexdigest() + "  " + destination.name + "\n"
    )
    print(f"checksum_ok database={destination.name} sha256={digest.hexdigest()}", flush=True)
    print(f"verified_backup={destination.name} bytes={destination.stat().st_size}", flush=True)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--stall-seconds", type=float, default=120)
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--busy-timeout-ms", type=int, default=30_000)
    parser.add_argument("--pages", type=int, default=4096)
    args = parser.parse_args()
    try:
        backup_database(args.source, args.destination, max_seconds=args.max_seconds,
                        stall_seconds=args.stall_seconds, max_restarts=args.max_restarts,
                        busy_timeout_ms=args.busy_timeout_ms, pages=args.pages)
    except Exception as exc:
        print(f"backup_failed error_type={type(exc).__name__} reason={exc}",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


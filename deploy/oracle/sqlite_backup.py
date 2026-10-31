#!/usr/bin/env python3
"""Create and verify one consistent SQLite backup without copying a live file."""
import argparse
import hashlib
from pathlib import Path
import sqlite3
from time import monotonic


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if not source.is_file() or destination.exists():
        parser.error("source must exist and destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"backup_open source={source.name} bytes={source.stat().st_size}", flush=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(destination, timeout=30) as dst:
            last_report = [0.0]

            def progress(_status, remaining, total):
                now = monotonic()
                if remaining == 0 or now - last_report[0] >= 5:
                    copied = max(0, total - remaining)
                    percent = round(copied * 100 / total, 1) if total else 100.0
                    print(
                        f"backup_progress database={source.name} "
                        f"pages={copied}/{total} percent={percent}",
                        flush=True,
                    )
                    last_report[0] = now

            src.backup(dst, pages=4096, progress=progress, sleep=0.05)
            print(f"integrity_check_started database={destination.name}", flush=True)
            result = dst.execute("PRAGMA quick_check(1)").fetchone()[0]
            if result != "ok":
                raise RuntimeError("backup integrity check failed")
            print(f"integrity_check_ok database={destination.name}", flush=True)
    digest = hashlib.sha256()
    print(f"checksum_started database={destination.name}", flush=True)
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        digest.hexdigest() + "  " + destination.name + "\n"
    )
    print(f"checksum_ok database={destination.name} sha256={digest.hexdigest()}", flush=True)
    print(f"verified_backup={destination.name} bytes={destination.stat().st_size}")


if __name__ == "__main__":
    main()


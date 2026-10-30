#!/usr/bin/env python3
"""Create and verify one consistent SQLite backup without copying a live file."""
import argparse
import hashlib
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if not source.is_file() or destination.exists():
        parser.error("source must exist and destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(destination, timeout=30) as dst:
            src.backup(dst, pages=4096, sleep=0.05)
            result = dst.execute("PRAGMA quick_check(1)").fetchone()[0]
            if result != "ok":
                raise RuntimeError("backup integrity check failed")
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        digest.hexdigest() + "  " + destination.name + "\n"
    )
    print(f"verified_backup={destination.name} bytes={destination.stat().st_size}")


if __name__ == "__main__":
    main()


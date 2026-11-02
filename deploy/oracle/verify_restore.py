#!/usr/bin/env python3
"""Verify a transferred SQLite backup and rehearse restore to a NEW local file.

No SSH, service operations, credentials, or live database replacement.
"""
import argparse
import hashlib
import os
from pathlib import Path
import sqlite3


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def inventory(db):
    tables = [row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    return {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
            for name in tables}


def verify_restore(backup, destination):
    backup, destination = Path(backup).resolve(), Path(destination).absolute()
    expected = backup.with_suffix(backup.suffix + ".sha256").read_text().split()[0]
    if len(expected) != 64 or digest(backup) != expected:
        raise ValueError("Backup checksum mismatch")
    # Exclusive creation prevents overwriting a database or following a symlink.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as source:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            before = inventory(source)
            with sqlite3.connect(destination) as restored:
                source.backup(restored)
                if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored integrity check failed")
                if inventory(restored) != before:
                    raise ValueError("Restored table counts differ")
        if digest(backup) != expected:
            raise ValueError("Backup changed during rehearsal")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"status": "PASS", "tables_verified": len(before),
            "off_host_durability": "NOT_VERIFIED", "live_service_modified": False}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("new_restore_path", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_restore(args.backup, args.new_restore_path)))

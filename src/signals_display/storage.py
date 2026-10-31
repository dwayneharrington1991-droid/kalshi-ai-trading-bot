"""Short, serialized SQLite contexts that always release connections."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
import sqlite3
import threading

_locks = {}
_registry = threading.Lock()


@contextmanager
def connect(path, *, readonly=False):
    key = str(Path(path).resolve())
    with _registry:
        lock = _locks.setdefault(key, threading.RLock())
    with nullcontext() if readonly else lock:
        db = (
            sqlite3.connect(Path(key).as_uri() + "?mode=ro", uri=True, timeout=30)
            if readonly
            else sqlite3.connect(key, timeout=30)
        )
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            if readonly:
                db.execute("BEGIN")
            elif db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

"""Short, serialized SQLite contexts that always release connections."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading

_locks = {}
_registry = threading.Lock()


@contextmanager
def connect(path):
    key = str(Path(path).resolve())
    with _registry:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        db = sqlite3.connect(key, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=30000")
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

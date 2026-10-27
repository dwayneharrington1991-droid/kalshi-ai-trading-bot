"""Concurrent journal use, reliable close/rollback, and disconnected browsers."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import io
import pytest
from src.signals_display.storage import connect
from src.signals_display.server import make_handler


def test_parallel_connections_preserve_history_and_release_locks(tmp_path):
    path = tmp_path / "journal.db"
    with connect(path) as db:
        db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO history VALUES (0,'original')")

    def writer(i):
        with connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO history VALUES (?,?)", (i, str(i)))

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(writer, range(1, 121)))
    with connect(path) as db:
        assert db.execute("select count(*) from history").fetchone()[0] == 121
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("select 1")
    with pytest.raises(RuntimeError):
        with connect(path) as db:
            db.execute("INSERT INTO history VALUES (999,'rollback')")
            raise RuntimeError("interrupted")
    with connect(path) as db:
        assert db.execute("select count(*) from history").fetchone()[0] == 121


def test_client_disconnect_is_caught_before_server_traceback(monkeypatch):
    from http.server import BaseHTTPRequestHandler

    handler = object.__new__(make_handler(None))
    for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):

        def broken(self):
            raise error("client disconnected")

        monkeypatch.setattr(BaseHTTPRequestHandler, "handle", broken)
        handler.handle()


def test_duplicate_port_is_rejected_before_creating_second_journal(tmp_path):
    import subprocess, sys, socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    path = tmp_path / "must-not-be-created.db"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "src.signals_display.server",
                "--mode",
                "replay",
                "--port",
                str(listener.getsockname()[1]),
                "--journal",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0 and "already in use" in result.stderr
        assert not path.exists()
    finally:
        listener.close()


def test_wal_history_snapshot_does_not_block_market_writer(tmp_path):
    from threading import Event

    path = tmp_path / "j.db"
    with connect(path) as db:
        db.execute("CREATE TABLE samples (id INTEGER)")
        db.execute("INSERT INTO samples VALUES (1)")
    ready = Event()
    release = Event()

    def reader():
        with connect(path, readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
            ready.set()
            release.wait(5)
            return db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def writer():
        with connect(path) as db:
            db.execute("INSERT INTO samples VALUES (2)")

    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(reader)
        assert ready.wait(3)
        try:
            pool.submit(writer).result(timeout=3)
        finally:
            release.set()
        assert read.result(timeout=3) == 1
    with connect(path, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO samples VALUES (3)")

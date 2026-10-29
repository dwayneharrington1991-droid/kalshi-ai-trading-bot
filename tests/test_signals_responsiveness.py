"""Bounded history and live HTTP responsiveness during blocked background jobs."""
from datetime import timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from time import perf_counter
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from src.signals_display.monitor import Journal, PublicFeed, analyze, utcnow
from src.signals_display.runtime import CachedReport, LatestPayload
from src.signals_display.server import Monitor, StartupStatus, make_handler, make_startup_handler
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def test_startup_health_responds_before_database_or_feed_initialization():
    """A stalled initializer must not leave the local health socket silent."""
    from urllib.request import ProxyHandler, build_opener

    startup = StartupStatus("abc123")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_startup_handler(startup))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    try:
        with pytest.raises(HTTPError) as captured:
            opener.open(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=1
            )
        assert captured.value.code == 503
        value = json.load(captured.value)
        assert value["status"] == "STARTING"
        assert value["build_commit"] == "abc123"
        assert value["signals_only"] is True
        assert value["started_at"]
        with opener.open(f"http://127.0.0.1:{server.server_port}/", timeout=1) as response:
            page = response.read()
        assert response.status == 200 and b"Your market prediction helper" in page
        startup.update("INTELLIGENCE", "Opening preserved journals")
        with opener.open(
            f"http://127.0.0.1:{server.server_port}/api/state?view=main", timeout=1
        ) as response:
            state = json.load(response)
        assert state["runtime"]["startup"]["stage"] == "INTELLIGENCE"
        assert state["stale"] is True and state["direction"] == "UNCLEAR"
        assert state["scalp"] == "WAIT"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_startup_failure_reports_exact_stage_without_secret_message():
    startup = StartupStatus("abc123")
    startup.update("SIGNAL_JOURNAL", "Opening preserved journal")
    startup.fail("SIGNAL_JOURNAL", RuntimeError("secret-like exception detail"))
    value = startup.snapshot()
    assert value["stage"] == "FAILED_SIGNAL_JOURNAL"
    assert value["message"] == "Initialization failed: RuntimeError"
    assert "secret-like" not in json.dumps(value)


def test_large_journal_never_builds_missing_indexes_during_live_startup(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "large.db")
    with journal.connect() as db:
        db.execute("DROP INDEX IF EXISTS predictions_mode_id")

    real_stat = Path.stat

    def large_stat(path, *args, **kwargs):
        value = real_stat(path, *args, **kwargs)
        if Path(path) == Path(journal.path):
            from types import SimpleNamespace
            return SimpleNamespace(**{
                name: getattr(value, name) for name in dir(value)
                if name.startswith("st_") and name != "st_size"
            }, st_size=513 * 1024 * 1024)
        return value

    monkeypatch.setattr(Path, "stat", large_stat)
    result = journal.ensure_performance_indexes()
    assert result["status"] == "DEFERRED_LARGE_LIVE_JOURNAL"
    assert "predictions_mode_id" in result["missing"]
    with journal.read() as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='predictions_mode_id'"
        ).fetchone() is None


def test_report_cache_returns_immediately_and_never_queues_duplicates():
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return {"sample_count": 7}

    cache = CachedReport(slow, interval=60)
    try:
        assert cache.get()["status"] == "CALCULATING"
        assert started.wait(2)
        for _ in range(10):
            assert cache.get()["status"] == "CALCULATING"
        assert len(calls) == 1
        release.set()
        cache.future.result(timeout=2)
        report = cache.get()
        assert report["status"] == "READY" and report["value"]["sample_count"] == 7
        report["value"]["sample_count"] = 0
        assert cache.get()["value"]["sample_count"] == 7
    finally:
        release.set()
        cache.close()


def test_market_mailbox_coalesces_old_frames_without_a_backlog():
    box = LatestPayload()
    for i in range(100):
        box.put({"tick": i})
    sequence, frame = box.get(0)
    assert sequence == 100 and frame == {"tick": 99}
    assert box.get(sequence, timeout=0) == (100, None)


def test_dead_public_collector_is_unhealthy_even_when_prediction_state_still_ticks(tmp_path):
    monitor = Monitor(
        PublicFeed(lambda *_args, **_kwargs: None, multi_venue=False),
        Journal(tmp_path / "worker.db"),
    )

    class Worker:
        def __init__(self, alive): self.alive = alive
        def is_alive(self): return self.alive

    collector, prediction = Worker(True), Worker(True)
    monitor.worker_threads.update(collector=collector, prediction=prediction)
    monitor.worker_event("collector", "started")
    monitor.worker_event("collector", "completed")
    monitor.worker_event("prediction", "started")
    monitor.worker_event("prediction", "completed")
    assert monitor.workers_health()["healthy"] is True

    collector.alive = False
    health = monitor.workers_health()
    assert health["healthy"] is False
    assert health["workers"]["prediction"]["thread_alive"] is True
    assert "collector worker is not alive" in health["reasons"]

    monitor.state_version = 1
    monitor.last_tick = utcnow()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        with pytest.raises(HTTPError) as captured:
            urlopen(f"http://127.0.0.1:{server.server_port}/healthz", timeout=2)
        assert captured.value.code == 503
        payload = json.load(captured.value)
        assert payload["status"] == "UNHEALTHY"
        assert payload["workers"]["healthy"] is False
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_state_version_wait_wakes_immediately_without_database_or_model_work(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "j.db"))
    observed = []

    def wait():
        observed.append(monitor.wait_for_version(0, 2)[0])

    thread = threading.Thread(target=wait)
    thread.start()
    with monitor.lock:
        monitor.state_version = 1
        monitor.last_tick = NOW
        monitor.lock.notify_all()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert observed == [1]


def test_history_pages_are_indexed_bounded_and_do_not_run_research(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "j.db")
    with journal.connect() as db:
        db.executemany(
            "INSERT INTO predictions(mode,ticker,timestamp,payload) VALUES (?,?,?,?)",
            [
                ("REPLAY / SIMULATED", "KXBTC15M-MOCK", str(i), '{"unneeded":"blob"}')
                for i in range(5000)
            ],
        )
    monkeypatch.setattr(journal, "performance", lambda: pytest.fail("Research on history page"))
    one = journal.history_page(limit=20)
    two = journal.history_page(before_id=one["pagination"]["next_before_id"], limit=20)
    assert len(one["predictions"]) == len(two["predictions"]) == 20
    assert one["predictions"][0]["id"] == 5000
    assert two["predictions"][0]["id"] == 4980
    assert not {r["id"] for r in one["predictions"]} & {r["id"] for r in two["predictions"]}
    with journal.read() as db:
        plan = " ".join(
            str(tuple(r))
            for r in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM predictions WHERE id<? ORDER BY id DESC LIMIT ?",
                (5000, 20),
            )
        )
        assert "SEARCH" in plan and "INTEGER PRIMARY KEY" in plan
        assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 5000


def test_fast_main_state_and_pagination_during_blocked_research(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "j.db"))
    monitor.state = analyze(frame(), NOW)
    monitor.state_version = 1
    monitor.state["model"] = {"version": "test", "coverage": {"raw": "x" * 150000}}
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        assert release.wait(5)
        return {"validated": False}

    monitor.research_report.calculate = slow
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path):
        with urlopen(base + path, timeout=2) as response:
            return json.load(response)

    try:
        assert get("/api/research")["status"] == "CALCULATING"
        assert started.wait(2)
        timer = perf_counter()
        data = get("/api/state?view=main")
        assert perf_counter() - timer < 1
        assert "coverage" not in data["model"] and data["market"]
        assert "connection_diagnostics" not in data and "streaming" not in data
        assert "blockers" not in data["scalping_monitor"]
        assert "checks" not in data["scalping_monitor"]
        assert len(json.dumps(data)) < 30000
        assert get("/api/history-page?limit=20")["pagination"]["limit"] == 20
        assert len(json.dumps(get("/api/state?view=details"))) > 150000
        version = get("/api/state-version?after=0&timeout=1")
        assert version["changed"] is True
        assert version["state_version"] == monitor.state_version
        for path in ("/api/history-page?limit=101", "/api/history-page?before_id=-1"):
            with pytest.raises(HTTPError) as error:
                get(path)
            assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            get("/api/account-history")
        assert error.value.code == 403  # Public mode never exposes a saved private account.
    finally:
        release.set()
        monitor.research_report.close()
        monitor.engine_report.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_indexed_settlement_queue_migrates_without_altering_predictions(tmp_path):
    journal = Journal(tmp_path / "j.db")
    before = analyze(frame(), NOW)
    # A legacy record, predating the queue migration.
    with journal.connect() as db:
        db.execute(
            "INSERT INTO predictions(mode,ticker,timestamp,payload) VALUES (?,?,?,?)",
            ("PUBLIC MARKET DATA", before["market"]["ticker"], NOW.isoformat(), json.dumps(before)),
        )
    journal.seed_settlement_queue()
    assert journal.due_settlements(NOW) == []
    assert journal.due_settlements(NOW + timedelta(minutes=11)) == [before["market"]["ticker"]]
    journal.settle(before["market"]["ticker"], "yes", "PUBLIC MARKET DATA")
    assert journal.due_settlements(NOW + timedelta(minutes=11)) == []
    journal.seed_settlement_queue()
    with journal.read() as db:
        row = db.execute("SELECT payload,settlement_up FROM predictions").fetchone()
        assert json.loads(row[0]) == before and row[1] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_raw_feed_is_archived_once_without_bloating_new_prediction_rows(tmp_path):
    journal = Journal(tmp_path / "j.db")
    state = analyze(frame(), NOW)
    raw = {"status": "CONNECTED", "data": {"observed_at": NOW.isoformat(), "book": "x" * 100000}}
    state["model"] = {"coverage": {"Mock venue": raw}, "version": "experimental"}
    journal.record_feeds(state["model"]["coverage"])
    journal.step(state)
    with journal.read() as db:
        stored = db.execute("SELECT payload FROM predictions").fetchone()[0]
        assert len(stored) < 30000
        ref = json.loads(stored)["model"]["coverage"]["Mock venue"]["raw_observation"]
        recovered = db.execute(
            "SELECT payload FROM feed_observations WHERE source=? AND timestamp=?",
            (ref["source"], ref["timestamp"]),
        ).fetchone()[0]
        assert json.loads(recovered) == raw
    assert state["model"]["coverage"]["Mock venue"] == raw  # Live view unchanged.


def test_prediction_evidence_is_bounded_even_when_live_diagnostics_are_large(tmp_path):
    journal = Journal(tmp_path / "j.db")
    state = analyze(frame(), NOW)
    state["model"] = {
        "version": "bounded-test",
        "candidate_features": {"values": {"return_1m": 0.01}},
        "coverage": {"large": {"status": "LIVE", "debug": "x" * 2_000_000}},
    }
    journal.step(state)
    with journal.read() as db:
        raw = db.execute("SELECT payload FROM predictions").fetchone()[0]
    saved = json.loads(raw)
    assert len(raw) < 65536
    assert saved["schema"] == "compact-prediction-evidence-v1"
    assert saved["model"]["candidate_features"]["values"]["return_1m"] == 0.01
    assert "debug" not in raw


def test_storage_health_is_constant_time_and_preserves_database(tmp_path):
    journal = Journal(tmp_path / "j.db")
    before = Path(journal.path).stat().st_size
    health = journal.storage_health()
    assert health["database_bytes"] == before
    assert health["status"] == "NORMAL"
    assert Path(journal.path).exists()

"""Phase 5 performance repair: less duplicate work, identical results."""
import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import strategy_paper
from src.signals_display.monitor import Journal, utcnow
from src.signals_display.runtime import BACKGROUND_NICE, CachedReport, background_pool
from src.signals_display.server import Monitor


def _signal(db, i, created):
    db.execute("INSERT INTO strategy_paper_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (f"k{i}", f"T{i}", "SCALP" if i % 2 else "HOLD", "up", created, created,
                .5, .6, .4, 1, .5, .5, .1, .55, "{}"))


def test_strategy_performance_cache_is_identical_to_full_recomputation(tmp_path):
    journal = Journal(tmp_path / "j.db")
    asof = "2026-11-18T12:00:00+00:00"

    def fresh(when=asof):
        with journal.read() as db:
            return strategy_paper.performance(db, asof=when)

    assert journal.strategy_performance(asof) == fresh()
    with journal.connect() as db:
        _signal(db, 1, "2026-11-18T10:00:00+00:00")
    assert journal.strategy_performance(asof) == fresh()           # new signal
    with journal.connect() as db:
        db.execute("INSERT INTO strategy_paper_outcomes VALUES (?,?,?,?,?,?,?)",
                   ("k1", asof, "TARGET", .6, .08, 1, "{}"))
    assert journal.strategy_performance(asof) == fresh()           # new outcome
    with journal.connect() as db:
        db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", ("T1", "official", asof, 1))
    assert journal.strategy_performance(asof) == fresh()           # coverage denominator
    later = "2026-11-19T00:00:01+00:00"
    assert journal.strategy_performance(later) == fresh(later)      # today's P&L rolls over
    assert journal.strategy_performance(later)["HOLD"]["today_net_profit"] == 0
    # A caller mutating its copy can never corrupt the cached value.
    value = journal.strategy_performance(asof)
    value["portfolio"]["closed_trades"] = 999
    assert journal.strategy_performance(asof) == fresh()


def test_strategy_paper_tables_stay_insert_only():
    # The cache's correctness depends on these immutability triggers.
    source = strategy_paper.__file__
    text = open(source).read()
    for trigger in ("strategy_paper_signals_no_update", "strategy_paper_signals_no_delete",
                    "strategy_paper_outcomes_no_update", "strategy_paper_outcomes_no_delete"):
        assert trigger in text


class _NoFeed:
    def poll(self):
        raise RuntimeError("not used")


def _ready_from_snapshot(monitor):
    state = monitor.snapshot()
    health = state.get("market_health", {})
    return bool(not state.get("stale", True) and health.get("contract_quotes_fresh")
                and health.get("reference_fresh") and state.get("market"))


def _ready_from_view(monitor):
    state = monitor.readiness_view()
    health = state["market_health"]
    return bool(not state["stale"] and health.get("contract_quotes_fresh")
                and health.get("reference_fresh") and state.get("market"))


@pytest.mark.parametrize("quote_age,ref_age,remaining,stale_flag,has_market,expected", [
    (1.0, 1.0, 300, False, True, True),
    (6.0, 1.0, 300, False, True, False),     # QUOTE_OLDER_THAN_5_SECONDS
    (1.0, 6.0, 300, False, True, False),     # REFERENCE_OLDER_THAN_5_SECONDS
    (6.0, 6.0, 300, False, True, False),     # both (the failed job's reasons)
    (1.0, 1.0, -1, False, True, False),      # contract expired
    (1.0, 1.0, 300, True, True, False),      # tick already judged stale
    (1.0, 1.0, 300, False, False, False),    # no active contract
    (-2.0, 1.0, 300, False, True, False),    # quote timestamp in the future
])
def test_readyz_view_matches_full_snapshot_decision(tmp_path, quote_age, ref_age, remaining,
                                                     stale_flag, has_market, expected):
    monitor = Monitor(_NoFeed(), Journal(tmp_path / "j.db"))
    now = utcnow()
    market = {
        "ticker": "KXBTC15M-TEST",
        "opens_at": (now - timedelta(minutes=5)).isoformat(),
        "closes_at": (now + timedelta(seconds=remaining)).isoformat(),
        "observed_at": (now - timedelta(seconds=quote_age)).isoformat(),
        "status": "open",
    }
    with monitor.lock:
        monitor.state = {
            **deepcopy(monitor.state),
            "mode": "PUBLIC MARKET DATA",
            "now": now.isoformat(),
            "stale": stale_flag,
            "market": market if has_market else None,
            "reference": {"price": 100000.0,
                          "observed_at": (now - timedelta(seconds=ref_age)).isoformat()},
        }
        monitor.last_tick = now
    view = _ready_from_view(monitor)
    assert view is expected
    assert view == _ready_from_snapshot(monitor)


def test_readyz_route_no_longer_builds_the_full_dashboard_snapshot():
    import inspect
    from src.signals_display import server
    source = inspect.getsource(server.make_handler)
    block = source[source.index('route == "/readyz"'):source.index('route == "/health"')]
    assert "monitor.readiness_view()" in block and "monitor.snapshot()" not in block


@pytest.mark.skipif(not hasattr(os, "getpriority"), reason="POSIX thread priority only")
def test_background_workers_run_at_lower_priority_and_market_threads_do_not():
    main_tid = threading.get_native_id()
    before = os.getpriority(os.PRIO_PROCESS, main_tid)
    pool = background_pool(1, "test-background")
    try:
        worker = pool.submit(lambda: os.getpriority(os.PRIO_PROCESS, threading.get_native_id())).result()
    finally:
        pool.shutdown()
    assert worker >= BACKGROUND_NICE
    assert os.getpriority(os.PRIO_PROCESS, main_tid) == before
    report = CachedReport(lambda: os.getpriority(os.PRIO_PROCESS, threading.get_native_id()), interval=0)
    try:
        report.get()
        report.future.result()
        assert report.get()["value"] >= BACKGROUND_NICE
    finally:
        report.close()

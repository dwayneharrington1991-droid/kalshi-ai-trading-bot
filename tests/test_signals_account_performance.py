"""Bounded account/manual views retain full confirmed and user-reported history."""
from datetime import timedelta

from src.signals_display import account_state
from src.signals_display.account_state import AccountJournal
from src.signals_display.manual import ManualJournal
from tests.test_signals_account_readonly import fill, snap
from tests.test_signals_display import NOW


def historical_cycles(count):
    rows = []
    for i in range(count):
        opened = fill(f"open-{i:04d}", q="1", fees=".01")
        closed = fill(f"close-{i:04d}", q="1", action="sell", price=".60", fees=".01")
        for record, offset in ((opened, 0), (closed, 1)):
            record["ticker"] = f"KXBTC15M-HISTORY-{i:04d}"
            record["created_time"] = (NOW - timedelta(seconds=2 * (count - i) - offset)).isoformat()
        rows.extend((opened, closed))
    return rows


def test_account_snapshots_reuse_worker_built_fifo_and_cannot_corrupt_cache(tmp_path, monkeypatch):
    store = AccountJournal(tmp_path / "account.db")
    rows = []
    for i in range(2576):
        row = fill(str(i), q="1", fees=".01")
        row["ticker"] = f"KXBTC15M-{i % 10}"
        rows.append(row)
    data = snap(fills=rows)
    data["positions"] = [
        {
            "ticker": f"KXBTC15M-{i}",
            "position_fp": str(sum(r["ticker"] == f"KXBTC15M-{i}" for r in rows)),
        }
        for i in range(10)
    ]
    store.reconcile(data)

    def forbidden(_):
        raise AssertionError("Browser/account snapshot must not rebuild full FIFO history")

    monkeypatch.setattr(account_state, "confirmed_cycles", forbidden)
    for _ in range(20):
        value = store.snapshot()
        assert len(value["holdings"]) == 10
        assert value["fill_count"] == 2576
        assert len(value["recent_fills"]) == 50
        value["holdings"][0]["quantity"] = -999
    assert store.snapshot()["holdings"][0]["quantity"] > 0
    store.select(1, True)
    assert next(h for h in store.snapshot()["holdings"] if h["id"] == 1)["selected"]


def test_completed_account_history_is_bounded_paginated_and_survives_restart(tmp_path):
    path = tmp_path / "account.db"
    store = AccountJournal(path)
    store.reconcile(snap("0", historical_cycles(75)))
    state = store.snapshot()
    assert len(state["completed_trades"]) == 50
    assert state["completed_trades_total"] == 75
    assert state["completed_history_has_more"]
    page1 = store.completed_history(limit=20)
    page2 = store.completed_history(before=page1["next_before_id"], limit=20)
    assert len(page1["trades"]) == len(page2["trades"]) == 20
    assert not (
        {r["history_id"] for r in page1["trades"]} & {r["history_id"] for r in page2["trades"]}
    )
    restored = AccountJournal(path)
    assert restored.completed_history(limit=20) == page1
    all_rows = []
    cursor = None
    while True:
        page = restored.completed_history(before=cursor, limit=20)
        all_rows.extend(page["trades"])
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert len(all_rows) == 75
    assert all(abs(r["realized_pnl"] - 0.18) < 1e-9 for r in all_rows)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM monitor_account_fills").fetchone()[0] == 150


def test_manual_guidance_reads_only_open_entries_and_paginated_records(tmp_path):
    store = ManualJournal(tmp_path / "manual.db")
    with store.connect() as db:
        db.executemany(
            """INSERT INTO manual_entries(id,ticker,side,price,quantity,fees,entered_at,target,stop,max_hold,slippage,status)
            VALUES (?,?,'up',.4,1,.01,?,.05,.1,60,.01,'CLOSED')""",
            [(i, f"KXBTC15M-OLD-{i}", NOW.isoformat()) for i in range(1, 4001)],
        )
        db.executemany(
            "INSERT INTO manual_exits(entry_id,price,quantity,fees,exited_at,recorded_at) VALUES (?,.6,1,.01,?,?)",
            [(i, NOW.isoformat(), NOW.isoformat()) for i in range(1, 4001)],
        )
        db.execute("UPDATE manual_entries SET status='OPEN' WHERE id=4000")
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM manual_entries WHERE status='OPEN' ORDER BY id DESC"
        ).fetchall()
    assert any("manual_entries_status_id" in row[3] for row in plan)
    assert len(store.records(open_only=True)["entries"]) == 1
    guidance = store.guidance({}, NOW, {})
    assert len(guidance) == 1 and guidance[0]["guidance"] == "DATA STALE — CHECK POSITION"
    first = store.records(limit=20)
    second = store.records(before=first["next_before_id"], limit=20)
    assert len(first["entries"]) == len(second["entries"]) == 20
    assert first["entries"][-1]["id"] > second["entries"][0]["id"]
    assert len(store.records()["entries"]) == 4000


def test_cached_account_view_remains_responsive_during_reconciliation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap())
    entered, release = Event(), Event()
    original = account_state.confirmed_cycles

    def slow_history(fills):
        entered.set()
        assert release.wait(5)
        return original(fills)

    monkeypatch.setattr(account_state, "confirmed_cycles", slow_history)
    update = snap("3", [fill("added", q="1")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker = pool.submit(store.reconcile, update)
        try:
            assert entered.wait(2)
            # A browser request receives the last complete, timestamped view
            # rather than waiting for large history calculations.
            value = pool.submit(store.snapshot).result(timeout=0.5)
            assert value["holdings"][0]["quantity"] == 2
        finally:
            release.set()
        worker.result(timeout=2)
    assert store.snapshot()["holdings"][0]["quantity"] == 3
    assert store.snapshot()["holdings"][0]["basis_verified"]


def test_balance_only_sync_reuses_unchanged_fill_cycles(tmp_path, monkeypatch):
    store = AccountJournal(tmp_path / "account.db")
    data = snap("0", historical_cycles(5))
    store.reconcile(data)

    def forbidden(_):
        raise AssertionError("No new economic fills: retain cached FIFO history")

    monkeypatch.setattr(account_state, "confirmed_cycles", forbidden)
    data["balance"] = {"balance": 200000}
    store.reconcile(data)
    assert store.snapshot()["balance"]["balance"] == 200000
    assert store.snapshot()["completed_trades_total"] == 5

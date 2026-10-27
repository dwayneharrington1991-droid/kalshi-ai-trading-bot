"""Mixed historical fill formats are retained, resolved by authority, or quarantined."""
from copy import deepcopy
from collections import Counter
from datetime import timedelta
import json
import pytest
from src.signals_display.account_state import AccountJournal
from src.signals_display.monitor import Journal
from src.signals_display.server import Monitor
from tests.test_signals_current_api import canonical
from tests.test_signals_account_readonly import snap, fill
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def test_deprecated_conflict_is_explicitly_audited_with_canonical_basis(tmp_path):
    raw = canonical()
    raw.update(side="yes", action="sell")
    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap(fills=[raw]))
    state = store.snapshot()
    h = state["holdings"][0]
    assert h["basis_verified"] and h["entry_price"] == 0.4 and h["entry_fees"] == 0.02
    assert state["fill_reconciliation"]["canonical_legacy_discrepancies"] == 1
    with store.connect() as db:
        audit = db.execute("SELECT payload,status FROM monitor_account_fill_audit").fetchone()
    assert json.loads(audit[0]) == raw and audit[1] == "CANONICAL_WITH_LEGACY_WARNING"


def test_canonical_contradiction_preserves_positions_but_withholds_costs(tmp_path):
    raw = canonical()
    raw["book_side"] = "ask"
    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap(fills=[raw]))
    s = store.snapshot()
    h = s["holdings"][0]
    assert (
        h["quantity"] == 2
        and not h["basis_verified"]
        and h["entry_price"] is h["entry_fees"] is None
    )
    assert s["balance"]["balance"] == 100000 and s["fill_reconciliation"]["quarantined_fills"] == 1
    assert s["fill_count"] == 0
    with store.connect() as db:
        assert (
            json.loads(db.execute("SELECT payload FROM monitor_account_fill_audit").fetchone()[0])
            == raw
        )


def test_conflicting_duplicate_id_is_not_resolved_by_page_order(tmp_path):
    a = canonical()
    b = canonical(outcome="no", yes=".60", no=".40")
    for i, rows in enumerate(([a, b], [b, a])):
        store = AccountJournal(tmp_path / f"a{i}.db")
        store.reconcile(snap(fills=rows))
        s = store.snapshot()
        assert s["fill_count"] == 0 and not s["holdings"][0]["basis_verified"]
        assert (
            s["fill_reconciliation"]["audit_records"] == 2
            and s["fill_reconciliation"]["quarantined_fills"] == 1
        )


def test_quarantined_record_can_be_resolved_without_erasing_earlier_versions(tmp_path):
    store = AccountJournal(tmp_path / "a.db")
    good = canonical()
    store.reconcile(snap(fills=[good]))
    bad = deepcopy(good)
    bad["book_side"] = "ask"
    store.reconcile(snap(fills=[bad]))
    assert not store.snapshot()["holdings"][0]["basis_verified"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM monitor_account_fills").fetchone()[0] == 1
    store.reconcile(snap(fills=[good]))
    s = store.snapshot()
    assert s["holdings"][0]["basis_verified"] and s["fill_reconciliation"]["quarantined_fills"] == 0
    assert s["fill_reconciliation"]["audit_records"] == 2


def test_unknown_fill_direction_does_not_guess_or_claim_order_unfilled(tmp_path):
    raw = fill()
    raw.pop("action")
    data = snap(fills=[raw])
    data["orders"] = [
        dict(order_id="order1", ticker="KXBTC15M-MOCK", status="resting", remaining_count_fp="8")
    ]
    store = AccountJournal(tmp_path / "a.db")
    store.reconcile(data)
    s = store.snapshot()
    assert not s["holdings"][0]["basis_verified"]
    assert s["pending_orders"][0]["confirmed_filled_quantity"] is None
    assert s["pending_orders"][0]["status"] == "FILL DETAILS UNVERIFIED"


def test_future_fill_is_quarantined_not_used_as_an_entry(tmp_path):
    raw = canonical()
    raw["created_time"] = (NOW + timedelta(seconds=20)).isoformat()
    store = AccountJournal(tmp_path / "a.db")
    store.reconcile(snap(fills=[raw]))
    assert store.snapshot()["fill_reconciliation"]["quarantined_fills"] == 1
    assert not store.snapshot()["holdings"][0]["basis_verified"]


def test_2576_historical_fills_and_10_positions_complete_verified_bootstrap(tmp_path):
    rows = []
    for i in range(2576):
        f = canonical(str(i), q="1", fee=".01")
        f["ticker"] = "KXBTC15M-MOCK-" + str(i % 10)
        if i % 17 == 0:
            f.update(side="yes", action="sell")
        rows.append(f)
    counts = Counter(f["ticker"] for f in rows)
    data = snap(fills=rows)
    data["positions"] = [{"ticker": t, "position_fp": str(q)} for t, q in counts.items()]

    class Client:
        credential_binding = "mock-account"

        def permissions(self):
            return {"read_only_verified": True}

        def snapshot(self):
            assert self.require_readonly
            return data

    m = Monitor(None, Journal(tmp_path / "journal.db"), account_store=tmp_path / "account.db")
    m.account_candidate = Client()
    m.update_account(NOW)
    m.account_job.result(timeout=15)
    m.update_account(NOW + timedelta(seconds=1))
    for ticker in counts:
        q = deepcopy(frame()["markets"][0])
        q["ticker"] = ticker
        q["quotes"]["up"]["bid_size"] = 10000
        m.manual_quotes[ticker] = q
    account = m.account_view({}, NOW + timedelta(seconds=1))
    assert (
        account["status"] == "READ_ONLY_CONNECTED" and account["available_balance_dollars"] == 1000
    )
    assert len(account["holdings"]) == 10 and account["fill_count"] == 2576
    assert all(h["basis_verified"] for h in account["holdings"])
    assert all(t["estimated_exit_net_pnl"] is not None for t in account["live_trade_cards"])
    assert account["fill_reconciliation"]["quarantined_fills"] == 0
    assert account["fill_reconciliation"]["canonical_legacy_discrepancies"] > 0

"""Mock authenticated GETs only. Never reads real credentials or contacts Kalshi."""
from copy import deepcopy
from datetime import timedelta
import json

import httpx
import pytest

from src.signals_display.account_readonly import (
    ReadOnlyKalshi,
    AccountReadError,
    normalized_fill,
    PATHS,
)
from src.signals_display.account_state import AccountJournal, account_guidance, incremental_add
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def fill(identifier="fill1", q="2.00", side="yes", action="buy", price=".40", fees=".02"):
    return {
        "fill_id": identifier,
        "order_id": "order1",
        "ticker": "KXBTC15M-MOCK",
        "side": side,
        "action": action,
        "count_fp": q,
        side + "_price_dollars": price,
        "fee_cost_dollars": fees,
        "created_time": NOW.isoformat(),
    }


def snap(q="2.00", fills=None):
    return {
        "complete": True,
        "observed_at": NOW.isoformat(),
        "positions": [{"ticker": "KXBTC15M-MOCK", "position_fp": q}] if q != "0" else [],
        "fills": [fill()] if fills is None else fills,
        "orders": [],
        "balance": {"balance": 100000},
        "permissions": {"status": "PERMISSIONS_UNVERIFIED", "read_only_verified": False},
    }


def test_get_allowlist_signs_exact_path_and_never_accepts_mutations():
    messages = []
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, json={"balance": 100}, request=httpx.Request("GET", url))

    client = ReadOnlyKalshi("TEST_ONLY_KEY", lambda m: messages.append(m) or "TEST_SIGNATURE", get)
    assert client.request("GET", "/trade-api/v2/portfolio/balance")["balance"] == 100
    assert messages[0].endswith("GET/trade-api/v2/portfolio/balance")
    assert calls[0][1]["follow_redirects"] is False
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(AccountReadError):
            client.request(method, "/trade-api/v2/portfolio/orders")
    for path in (
        "/trade-api/v2/portfolio/transfers",
        "/trade-api/v2/portfolio/orders/id",
        "https://evil.example/portfolio/balance",
        "/trade-api/v2/portfolio/orders/../balance",
    ):
        with pytest.raises(AccountReadError):
            client.request("GET", path)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "scopes,expected",
    [
        (["read"], "READ_ONLY_SCOPE_REPORTED"),
        (["read", "write"], "TRADING_CAPABLE_KEY"),
        (None, "PERMISSIONS_UNVERIFIED"),
    ],
)
def test_permissions_are_verified_or_disclosed_not_assumed(scopes, expected):
    def get(url, **kwargs):
        return httpx.Response(
            200,
            json={"api_keys": [{"api_key_id": "TEST_ONLY_KEY", "scopes": scopes}]},
            request=httpx.Request("GET", url),
        )

    result = ReadOnlyKalshi("TEST_ONLY_KEY", lambda _: "fake", get).permissions()
    assert result["status"] == expected
    assert result["read_only_verified"] == (expected == "READ_ONLY_SCOPE_REPORTED")
    assert "TEST_ONLY_KEY" not in json.dumps(result)


def test_failed_account_response_never_discloses_auth_headers_or_body():
    def get(url, **kwargs):
        raise RuntimeError("private body TEST_ONLY_KEY TEST_SIGNATURE")

    client = ReadOnlyKalshi("TEST_ONLY_KEY", lambda _: "TEST_SIGNATURE", get)
    with pytest.raises(AccountReadError) as e:
        client.request("GET", "/trade-api/v2/portfolio/balance")
    assert "TEST_ONLY_KEY" not in str(e.value) and "TEST_SIGNATURE" not in str(e.value)


def test_repeated_pagination_cannot_produce_complete_snapshot():
    def get(url, **kwargs):
        return httpx.Response(
            200,
            json={"market_positions": [], "cursor": "repeat"},
            request=httpx.Request("GET", url),
        )

    with pytest.raises(AccountReadError, match="Repeated"):
        ReadOnlyKalshi("fake", lambda _: "fake", get).snapshot()


def test_confirmed_partial_fills_reconcile_without_assuming_order_fill(tmp_path):
    store = AccountJournal(tmp_path / "account.db")
    first = snap()
    first["orders"] = [
        {
            "order_id": "order1",
            "status": "resting",
            "initial_count_fp": "10",
            "fill_count_fp": "2",
            "remaining_count_fp": "8",
        }
    ]
    store.reconcile(first)
    a = store.snapshot()
    h = a["holdings"][0]
    assert h["quantity"] == 2 and h["entry_price"] == 0.4 and h["entry_fees"] == 0.02
    assert h["basis_verified"] and not h["selected"]
    store.select(h["id"], True)
    second = snap("5.00", [fill(), fill("fill2", "3.00", price=".50", fees=".03")])
    store.reconcile(second)
    h = store.snapshot()["holdings"][0]
    assert (
        h["quantity"] == 5 and h["entry_price"] == pytest.approx(0.46) and h["entry_fees"] == 0.05
    )
    # Position is authoritative during fill lag; cost basis is withheld, not invented.
    store.reconcile(snap("6.00", []))
    h = store.snapshot()["holdings"][0]
    assert h["quantity"] == 6 and not h["basis_verified"]
    assert store.snapshot()["fill_count"] == 2


def test_manual_reductions_and_exits_require_confirmed_account_positions(tmp_path):
    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap())
    sold = fill("sell", "1.00", action="sell", price=".60", fees=".01")
    store.reconcile(snap("1.00", [sold]))
    h = store.snapshot()["holdings"][0]
    assert h["quantity"] == 1 and h["entry_price"] == 0.4 and h["entry_fees"] == 0.01
    before = store.snapshot()
    incomplete = snap("0")
    incomplete["complete"] = False
    with pytest.raises(AccountReadError):
        store.reconcile(incomplete)
    assert store.snapshot()["holdings"][0]["status"] == "OPEN"
    store.reconcile(snap("0", []))
    assert store.snapshot()["holdings"][0]["status"] == "CLOSED"
    assert any(
        e["kind"] == "CONFIRMED_EXIT_OR_ZERO_POSITION" for e in store.snapshot()["recent_events"]
    )


def test_missing_fill_fees_mean_unknown_basis_not_zero_fees(tmp_path):
    store = AccountJournal(tmp_path / "account.db")
    f = fill()
    del f["fee_cost_dollars"]
    store.reconcile(snap(fills=[f]))
    assert not store.snapshot()["holdings"][0]["basis_verified"]


def test_side_change_creates_new_unselected_holding_and_preserves_history(tmp_path):
    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap())
    store.select(1, True)
    store.reconcile(snap("-1.00", [fill("net-no", "3.00", side="no", price=".6", fees=".03")]))
    records = store.snapshot()["holdings"]
    new = next(h for h in records if h["status"] == "OPEN")
    assert new["side"] == "down" and not new["selected"]
    assert any(h["id"] == 1 and h["status"] == "CLOSED" for h in records)


def test_restart_preserves_selection_but_requires_fresh_account_and_quotes(tmp_path):
    path = tmp_path / "a.db"
    store = AccountJournal(path)
    store.reconcile(snap())
    store.select(1, True)
    restored = AccountJournal(path).snapshot()
    h = restored["holdings"][0]
    defaults = {"target": 0.05, "stop": 0.10, "horizon": 60, "slippage": 0.01}
    q = frame()["markets"][0]
    result = account_guidance(h, restored, q, {}, NOW, defaults, False)
    assert result["guidance"] == "WAIT" and "reconciliation" in result["reason"]
    result = account_guidance(h, restored, q, {}, NOW, defaults, True)
    assert result["guidance"] == "HOLD" and result["executable_bid"] == 0.48
    q["quotes"]["up"].update(bid=0.60, ask=0.62)
    assert account_guidance(h, restored, q, {}, NOW, defaults, True)["guidance"] == "CONSIDER EXIT"
    assert (
        AccountJournal(path).snapshot()["holdings"][0]["status"] == "OPEN"
    )  # Guidance never closes holdings.


def test_missing_limits_and_a_losing_position_never_trigger_adding(tmp_path):
    store = AccountJournal(tmp_path / "a.db")
    store.reconcile(snap())
    a = store.snapshot()
    h = a["holdings"][0]
    result = incremental_add(h, a, frame()["markets"][0], {}, {"slippage": 0.01}, NOW)
    assert not result["eligible"] and "limits" in result["reason"]
    a["limits"] = {
        "max_add_quantity": 1,
        "max_per_contract_quantity": 10,
        "max_account_quantity": 20,
        "max_add_cost_dollars": 1,
        "minimum_cash_reserve": 100,
    }
    state = {
        "stale": False,
        "market": frame()["markets"][0],
        "model": {},
        "estimate": {"settlement_up": 0.99},
    }
    result = incremental_add(h, a, frame()["markets"][0], state, {"slippage": 0.01}, NOW)
    assert not result["eligible"] and "evidence" in result["reason"]


def test_incremental_add_checks_explicit_limits_and_not_loss_state(tmp_path):
    store = AccountJournal(tmp_path / "a.db")
    store.reconcile(snap())
    a = store.snapshot()
    h = a["holdings"][0]
    a["limits"] = {
        "max_add_quantity": 1,
        "max_per_contract_quantity": 10,
        "max_account_quantity": 20,
        "max_add_cost_dollars": 1,
        "minimum_cash_reserve": 100,
    }
    state = {
        "stale": False,
        "market": frame()["markets"][0],
        "model": {
            "settlement_probability_sensitivity": [0.85, 0.95],
            "learning_status": {
                "trained_candidate_loaded": True,
                "heldout_report": {
                    "test": {
                        "selected_candidate": {"samples": 120, "brier": 0.10, "log_loss": 0.3},
                        "zero_drift": {"brier": 0.2, "log_loss": 0.6},
                    }
                },
            },
        },
    }
    result = incremental_add(h, a, frame()["markets"][0], state, {"slippage": 0.01}, NOW)
    assert result["eligible"] and result["incremental_expected_net_pnl"] > 0
    a["limits"]["max_per_contract_quantity"] = 2
    assert not incremental_add(h, a, frame()["markets"][0], state, {"slippage": 0.01}, NOW)[
        "eligible"
    ]


def test_account_key_binding_cannot_mix_two_accounts(tmp_path):
    store = AccountJournal(tmp_path / "a.db")
    store.bind("test-key-hash-one")
    store.bind("test-key-hash-one")
    with pytest.raises(AccountReadError, match="binding changed"):
        store.bind("test-key-hash-two")


def test_live_cards_use_filled_quantity_not_unfilled_orders_and_unknown_is_not_zero(tmp_path):
    from src.signals_display.account_state import live_trade_cards

    store = AccountJournal(tmp_path / "card.db")
    data = snap()
    data["orders"] = [
        {
            "order_id": "order1",
            "ticker": "KXBTC15M-MOCK",
            "status": "resting",
            "remaining_count_fp": "8",
        }
    ]
    store.reconcile(data)
    a = store.snapshot()
    defaults = {"target": 0.05, "stop": 0.10, "horizon": 60, "slippage": 0.01}
    quote = frame()["markets"][0]
    cards = live_trade_cards(a, {"KXBTC15M-MOCK": quote}, {}, NOW, defaults, True)
    assert len(cards) == 1 and cards[0]["filled_quantity"] == 2
    assert cards[0]["total_entry_cost"] == pytest.approx(0.82)
    assert cards[0]["estimated_exit_net_pnl"] == pytest.approx(0.08)
    assert cards[0]["conditional_winning_settlement_net_profit"] == pytest.approx(1.18)
    assert cards[0]["potential_target_net_profit"] >= 0.10 - 1e-9
    assert (
        a["pending_orders"][0]["confirmed_filled_quantity"] == 2
        and a["pending_orders"][0]["invested_from_unfilled_quantity"] == 0
    )
    f = fill()
    del f["fee_cost_dollars"]
    other = AccountJournal(tmp_path / "unknown.db")
    other.reconcile(snap(fills=[f]))
    card = live_trade_cards(other.snapshot(), {"KXBTC15M-MOCK": quote}, {}, NOW, defaults, True)[0]
    assert (
        card["average_entry_price"] == 0.4
        and card["entry_fees"] is None
        and card["total_entry_cost"] is None
    )
    stale = live_trade_cards(a, {"KXBTC15M-MOCK": quote}, {}, NOW, defaults, False)[0]
    assert (
        stale["values_stale"]
        and stale["guidance"] == "WAIT"
        and stale["current_executable_exit_price"] is None
    )


def test_actual_completed_fifo_results_remain_separate_from_simulations(tmp_path):
    store = AccountJournal(tmp_path / "cycles.db")
    store.reconcile(snap("5", [fill(), fill("add", "3", price=".50", fees=".03")]))
    store.reconcile(snap("4", [fill("sell1", "1", action="sell", price=".60", fees=".01")]))
    store.reconcile(snap("0", [fill("sell2", "4", action="sell", price=".70", fees=".04")]))
    trades = store.snapshot()["completed_trades"]
    assert len(trades) == 1 and trades[0]["realized_pnl"] == pytest.approx(1.0)
    assert trades[0]["total_entry_cost"] == pytest.approx(2.35)
    assert "confirmed-fill" in trades[0]["source"]


def test_server_side_signing_with_generated_mock_key_never_needs_real_credentials(
    tmp_path, monkeypatch
):
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path = tmp_path / "mock-only.pem"
    path.write_bytes(pem)
    path.chmod(0o600)
    monkeypatch.setenv("KALSHI_MONITOR_KEY_ID", "GENERATED_MOCK_KEY")
    monkeypatch.delenv("KALSHI_MONITOR_PRIVATE_KEY_PEM", raising=False)
    monkeypatch.setenv("KALSHI_MONITOR_PRIVATE_KEY_PATH", str(path))
    client = ReadOnlyKalshi.from_environment()
    message = "123GET/trade-api/v2/portfolio/balance"
    signature = base64.b64decode(client._signer(message))
    key.public_key().verify(
        signature,
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    path.chmod(0o644)
    with pytest.raises(AccountReadError, match="owner-only"):
        ReadOnlyKalshi.from_environment()

"""Fixtures modeled on official Kalshi OpenAPI 3.35.0. No real keys/account calls."""
import base64
from datetime import timedelta
import json
import httpx
import pytest
from src.signals_display.account_readonly import ReadOnlyKalshi, normalized_fill, AccountReadError
from src.signals_display.account_state import AccountJournal
from src.signals_display.monitor import Journal
from src.signals_display.server import Monitor
from tests.test_signals_display import NOW
from tests.test_signals_account_readonly import snap


@pytest.mark.parametrize(
    "scopes,allowed",
    [
        (["read", "read::portfolio_balance"], True),
        (["read", "read::block_trade_accept"], True),
        (["read", "read"], True),
        (["read::portfolio_balance"], False),
        (["read", "write::trade"], False),
        (["read", "write::transfer"], False),
        (["read", "write::fcm_risk"], False),
        (["read", "write::block_trade_accept"], False),
        (["read", "read::unknown"], False),
    ],
)
def test_current_documented_scopes_preserve_strict_read_only_gate(scopes, allowed):
    def get(url, **kwargs):
        return httpx.Response(
            200,
            json={"api_keys": [{"api_key_id": "mock", "scopes": scopes}]},
            request=httpx.Request("GET", url),
        )

    result = ReadOnlyKalshi("mock", lambda m: "sig", get).permissions()
    assert result["read_only_verified"] is allowed
    if any(s.startswith("write::") for s in scopes):
        assert result["status"] == "TRADING_CAPABLE_KEY"


def test_ed25519_supported_auth_signs_same_exact_prehash_message(monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    monkeypatch.setenv("KALSHI_MONITOR_KEY_ID", "  mock-id  ")
    monkeypatch.setenv("KALSHI_MONITOR_PRIVATE_KEY_PEM", pem)
    client = ReadOnlyKalshi.from_environment()
    message = "123GET/trade-api/v2/portfolio/balance"
    key.public_key().verify(base64.b64decode(client._signer(message)), message.encode())
    assert client._key_id == "mock-id"


def canonical(identifier="f1", outcome="yes", yes=".40", no=".60", q="2.00", fee=".02"):
    return dict(
        fill_id=identifier,
        order_id="order1",
        ticker="KXBTC15M-MOCK",
        outcome_side=outcome,
        book_side="bid" if outcome == "yes" else "ask",
        count_fp=q,
        yes_price_dollars=yes,
        no_price_dollars=no,
        fee_cost=fee,
        created_time=NOW.isoformat(),
    )


def test_modern_fill_without_deprecated_fields_has_dollar_fees_and_correct_exposure():
    f = normalized_fill(canonical())
    assert f["side"] == "yes" and f["price"] == 0.4 and f["fees"] == 0.02
    f = normalized_fill(canonical(outcome="no", yes=".60", no=".40"))
    assert f["side"] == "no" and f["price"] == 0.4 and f["action"] == "buy"
    assert "economic normalization" in f["direction_basis"]


def test_modern_fill_audits_legacy_disagreement_but_rejects_canonical_conflict():
    raw = canonical()
    raw["side"] = "yes"
    assert normalized_fill(raw)["side"] == "yes"
    raw["action"] = "sell"
    resolved = normalized_fill(raw)
    assert (
        resolved["side"] == "yes"
        and resolved["direction_warning"] == "CANONICAL_LEGACY_DIRECTION_DISAGREEMENT"
    )
    raw = canonical()
    raw["book_side"] = "ask"
    with pytest.raises(AccountReadError, match="direction"):
        normalized_fill(raw)


def test_canonical_reductions_and_close_preserve_realized_costs_and_history(tmp_path):
    store = AccountJournal(tmp_path / "account.db")
    store.reconcile(snap(fills=[canonical()]))
    h = store.snapshot()["holdings"][0]
    assert h["quantity"] == 2 and h["entry_fees"] == 0.02
    reduce = canonical("f2", "no", ".60", ".40", "1.00", ".01")
    store.reconcile(snap("1.00", [reduce]))
    h = store.snapshot()["holdings"][0]
    assert h["quantity"] == 1 and h["entry_price"] == 0.4 and h["entry_fees"] == 0.01
    store.reconcile(snap("0", [canonical("f3", "no", ".70", ".30", "1.00", ".01")]))
    assert store.snapshot()["completed_trades"][0]["realized_pnl"] == pytest.approx(0.46)
    assert store.snapshot()["fill_count"] == 3


def test_all_account_gets_use_same_key_bound_subaccount():
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["params"]))
        data = (
            {"api_keys": [{"api_key_id": "mock", "scopes": ["read"], "subaccount": 3}]}
            if url.endswith("/api_keys")
            else {"market_positions": []}
            if url.endswith("/positions")
            else {"fills": []}
            if url.endswith("/fills")
            else {"orders": []}
            if url.endswith("/orders")
            else {"balance_dollars": "10.25"}
        )
        return httpx.Response(200, json=data, request=httpx.Request("GET", url))

    client = ReadOnlyKalshi("mock", lambda m: "sig", get)
    client.require_readonly = True
    client.snapshot()
    assert next(params for url, params in calls if url.endswith("/orders"))["status"] == "resting"
    assert len(calls) == 5 and all(
        params["subaccount"] == 3 for url, params in calls if "/portfolio/" in url
    )
    with pytest.raises(AccountReadError):
        client.request("GET", "/trade-api/v2/api_keys", {"subaccount": 3})


def test_off_status_distinguishes_unrequested_credentials_and_rejected_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_MONITOR_KEY_ID", "mock")
    monkeypatch.setenv("KALSHI_MONITOR_PRIVATE_KEY_PEM", "mock-never-loaded")
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    assert monitor.account_view({}, NOW)["status"] == "ACCOUNT_NOT_ENABLED"
    monitor.account_requested = True
    monitor.account_error = "KEY_LOAD_FAILED"
    assert monitor.account_view({}, NOW)["status"] == "ACCOUNT_BLOCKED"
    monkeypatch.delenv("KALSHI_MONITOR_KEY_ID")
    assert monitor.account_view({}, NOW)["status"] == "ACCOUNT_NOT_CONFIGURED"
    monitor.account_candidate = object()
    monitor.account_error = "HTTP 403"
    assert monitor.account_view({}, NOW)["status"] == "ACCOUNT_BLOCKED"


def test_only_empty_account_store_can_rotate_binding_after_verified_bootstrap(tmp_path):
    empty = AccountJournal(tmp_path / "empty.db")
    empty.bind("old")
    empty.bind("new", allow_empty_rotation=True)
    empty.bind("new")
    populated = AccountJournal(tmp_path / "existing.db")
    populated.bind("old")
    populated.reconcile(snap(fills=[canonical()]))
    with pytest.raises(AccountReadError, match="binding changed"):
        populated.bind("new", allow_empty_rotation=True)
    assert (
        populated.snapshot()["fill_count"] == 1
        and populated.snapshot()["holdings"][0]["quantity"] == 2
    )


@pytest.mark.parametrize(
    "side,outcome,book,yes,no",
    [
        ("yes", "no", "ask", ".60", ".40"),
        ("no", "yes", "bid", ".40", ".60"),
    ],
)
def test_legacy_sell_and_canonical_exposure_agree_without_invented_purchase(
    side, outcome, book, yes, no
):
    raw = canonical(outcome=outcome, yes=yes, no=no)
    raw.update(side=side, action="sell", book_side=book)
    f = normalized_fill(raw)
    assert f["side"] == outcome and f["price"] == 0.4
    assert f["direction_basis"].startswith("canonical outcome exposure")

"""Mocked connection failures, explicit gates, and independent browser refresh."""
import json
from datetime import timedelta
from concurrent.futures import Future
import httpx
import pytest
from src.signals_display.account_readonly import ReadOnlyKalshi, credential_status, AccountReadError
from src.signals_display.monitor import PublicFeed, Journal, analyze
from src.signals_display.server import Monitor
from tests.test_signals_monitor import frame
from tests.test_signals_display import NOW


@pytest.mark.parametrize("code", [401, 403, 429, 503])
def test_metadata_http_failure_retains_exact_redacted_blocker(code):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            code, json={"secret": "NEVER_PRINT_BODY"}, request=httpx.Request("GET", url)
        )

    p = ReadOnlyKalshi("NEVER_PRINT_KEY", lambda m: "NEVER_PRINT_SIGNATURE", get).permissions()
    assert f"HTTP {code}" in p["blocker"] and not p["read_only_verified"]
    assert p["authentication"] == "UNVERIFIED" and len(calls) == 1
    assert "NEVER_PRINT" not in json.dumps(p)


@pytest.mark.parametrize(
    "metadata,blocker",
    [
        ({}, "KEY_METADATA_SCHEMA_UNSUPPORTED"),
        ({"api_keys": []}, "CURRENT_KEY_NOT_IN_METADATA"),
        ({"api_keys": [{"id": "mock"}]}, "READ_ONLY_SCOPE_NOT_PROVEN"),
        ({"api_keys": [{"id": "mock", "scopes": ["read", "transfer"]}]}, "MUTATION_SCOPE_PRESENT"),
    ],
)
def test_missing_or_mutating_scope_is_never_assumed_read_only(metadata, blocker):
    def get(url, **kwargs):
        return httpx.Response(200, json=metadata, request=httpx.Request("GET", url))

    p = ReadOnlyKalshi("mock", lambda m: "sig", get).permissions()
    assert blocker in p["blocker"] and not p["read_only_verified"]
    assert p["authentication"] == "AUTHENTICATED_GET_SUCCEEDED"


def test_environment_diagnostics_are_booleans_only(monkeypatch):
    monkeypatch.setenv("KALSHI_MONITOR_KEY_ID", "DO_NOT_DISPLAY")
    monkeypatch.setenv("KALSHI_MONITOR_PRIVATE_KEY_PEM", "DO_NOT_DISPLAY")
    data = credential_status()
    assert data["KALSHI_MONITOR_KEY_ID"] is True and "DO_NOT_DISPLAY" not in json.dumps(data)


def test_failed_public_requests_are_recorded_without_response_body():
    def get(url, **kwargs):
        return httpx.Response(429, text="NEVER_PRINT_BODY", request=httpx.Request("GET", url))

    f = PublicFeed(get)
    with pytest.raises(httpx.HTTPStatusError):
        f._read("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    d = f.transport_diagnostics[-1]
    assert d["http_status"] == 429 and d["status"] == "FAILED"
    assert "NEVER_PRINT_BODY" not in json.dumps(d)


def test_candle_fetch_does_not_block_live_poll_on_slow_history():
    f = PublicFeed(lambda *a, **k: None)
    f.candle_pool = object()
    f.candle_started = NOW
    f.candle_job = Future()
    f.candle_cache = {"received_at": (NOW - timedelta(seconds=160)).isoformat(), "candles": []}
    assert f.sampled_candles(NOW)["received_at"] == f.candle_cache["received_at"]
    assert (
        not f.candle_job.done()
    )  # stale history retained with original timestamp, never relabeled fresh


def test_pending_auth_never_enables_account_and_retries_only_after_interval(tmp_path):
    class Candidate:
        def permissions(self):
            return {"read_only_verified": False, "blocker": "HTTP 403"}

        def snapshot(self):
            raise AssertionError("Must not read account before permission verification")

    m = Monitor(None, Journal(tmp_path / "journal.db"))
    m.account_candidate = Candidate()
    m.update_account(NOW)
    with pytest.raises(AccountReadError, match="HTTP 403"):
        m.account_job.result(timeout=5)
    m.update_account(NOW + timedelta(seconds=1))
    assert m.account_client is None and "HTTP 403" in m.account_error
    assert m.account_view({}, NOW)["status"] == "PUBLIC_ONLY"
    assert m.account_job is None


def test_verified_async_auth_reconciles_actual_mock_holdings_and_balance(tmp_path):
    from tests.test_signals_account_readonly import snap

    class Candidate:
        credential_binding = "mock-binding-only"

        def permissions(self):
            return {"read_only_verified": True, "status": "READ_ONLY_SCOPE_REPORTED"}

        def snapshot(self):
            assert self.require_readonly
            return snap()

    m = Monitor(None, Journal(tmp_path / "journal.db"))
    m.account_candidate = Candidate()
    m.update_account(NOW)
    m.account_job.result(timeout=5)
    m.update_account(NOW + timedelta(seconds=1))
    m.manual_quotes["KXBTC15M-MOCK"] = frame()["markets"][0]
    account = m.account_view({}, NOW + timedelta(seconds=1))
    assert (
        account["status"] == "READ_ONLY_CONNECTED" and account["available_balance_dollars"] == 1000
    )
    assert account["live_trade_cards"][0]["filled_quantity"] == 2
    assert account["live_trade_cards"][0]["average_entry_price"] == 0.4
    assert account["live_trade_cards"][0]["estimated_exit_net_pnl"] is not None

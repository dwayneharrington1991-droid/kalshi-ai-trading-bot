import asyncio
from datetime import datetime, timedelta

import pytest

from scripts.read_only_validate import (
    ENVIRONMENT_URLS, ReadOnlyAccountClient, ReadOnlyValidationError,
    run_read_only_validation, validate_read_only_safety,
)
from src.utils.database import Market


pytestmark = pytest.mark.asyncio


def environment(tmp_path, selected="demo", **overrides):
    key = tmp_path / f"{selected}.pem"
    key.write_text("mock-key", encoding="utf-8")
    values = {
        "KALSHI_ENVIRONMENT": selected,
        "KALSHI_API_KEY": "SECRET_API_KEY",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "false",
        "LIVE_ORDER_SUBMISSION_KILL_SWITCH": "true",
        "RECONCILIATION_SHADOW_MODE": "true",
        "READ_ONLY_ACCOUNT_VALIDATION": "true",
    }
    values.update(overrides)
    return values


class AccountClient:
    def __init__(self, selected="demo"):
        self.environment = selected
        self.base_url = ENVIRONMENT_URLS[selected]
        self.closed = False
        self.calls = {"balance": 0, "positions": 0, "orders": 0, "fills": 0}

    async def get_balance(self):
        self.calls["balance"] += 1
        return {"balance": 12345}

    async def get_positions(self):
        self.calls["positions"] += 1
        return {"market_positions": []}

    async def get_all_orders(self, **filters):
        self.calls["orders"] += 1
        return []

    async def get_all_fills(self, **filters):
        self.calls["fills"] += 1
        return []

    async def close(self):
        self.closed = True

    async def place_order(self, **kwargs):
        raise AssertionError("place_order reached")

    async def cancel_order(self, **kwargs):
        raise AssertionError("cancel_order reached")

    async def amend_order(self, **kwargs):
        raise AssertionError("amend_order reached")

    async def withdraw(self, **kwargs):
        raise AssertionError("withdraw reached")

    async def transfer(self, **kwargs):
        raise AssertionError("transfer reached")


async def fake_ingestion(manager, queue, kalshi_client):
    assert isinstance(kalshi_client, ReadOnlyAccountClient)
    for name in (
        "place_order", "cancel_order", "amend_order", "batch_create_orders",
        "withdraw", "withdrawal", "transfer",
    ):
        assert not hasattr(kalshi_client, name)
    await manager.upsert_markets([Market(
        market_id="READ-MKT", title="Read only", yes_price=.4, no_price=.6,
        volume=100, expiration_ts=int((datetime.now() + timedelta(days=1)).timestamp()),
        category="validation", status="active", last_updated=datetime.now(),
    )])


async def test_production_requires_explicit_read_only_flag(tmp_path):
    values = environment(tmp_path, "production", READ_ONLY_ACCOUNT_VALIDATION="false")
    with pytest.raises(ReadOnlyValidationError, match="READ_ONLY_ACCOUNT_VALIDATION=true"):
        validate_read_only_safety(values)


@pytest.mark.parametrize("overrides,reason", [
    ({"AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "true"}, "must remain disabled"),
    ({"AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "invalid"}, "must remain disabled"),
    ({"LIVE_ORDER_SUBMISSION_KILL_SWITCH": "false"}, "must remain enabled"),
    ({"RECONCILIATION_SHADOW_MODE": "false"}, "explicitly enabled"),
])
async def test_production_refuses_unsafe_gate_values(tmp_path, overrides, reason):
    with pytest.raises(ReadOnlyValidationError, match=reason):
        validate_read_only_safety(environment(tmp_path, "production", **overrides))


async def test_production_refusal_happens_before_client_construction(tmp_path):
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        return AccountClient("production")

    with pytest.raises(ReadOnlyValidationError):
        await run_read_only_validation(
            environment=environment(
                tmp_path, "production", READ_ONLY_ACCOUNT_VALIDATION="false"
            ),
            client_factory=factory,
            db_path=str(tmp_path / "refused.db"),
        )
    assert called is False


async def test_write_methods_are_absent_and_non_get_is_rejected():
    client = AccountClient()
    facade = ReadOnlyAccountClient(client, "demo")
    for name in (
        "place_order", "cancel_order", "amend_order", "batch_create_orders",
        "withdraw", "withdrawal", "transfer",
    ):
        assert not hasattr(facade, name)
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(ReadOnlyValidationError, match="GET requests only"):
            await facade._make_authenticated_request(verb, "/trade-api/v2/events")
    with pytest.raises(ReadOnlyValidationError, match="not approved"):
        await facade._make_authenticated_request("GET", "/trade-api/v2/withdrawals")
    with pytest.raises(ReadOnlyValidationError, match="not an approved"):
        await facade._ReadOnlyAccountClient__read("place_order")
    with pytest.raises(AttributeError):
        getattr(facade, "_ReadOnlyAccountClient__client")
    assert not hasattr(facade, "__dict__")


async def test_production_host_mismatch_closes_client(tmp_path):
    client = AccountClient("production")
    client.base_url = ENVIRONMENT_URLS["demo"]
    with pytest.raises(ReadOnlyValidationError, match="host mismatch"):
        await run_read_only_validation(
            environment=environment(tmp_path, "production"),
            client_factory=lambda environment: client,
        )
    assert client.closed is True


@pytest.mark.parametrize("selected", ["demo", "production"])
async def test_one_bounded_cycle_reads_account_and_closes(tmp_path, selected):
    client = AccountClient(selected)
    factory_calls = 0

    def factory(environment):
        nonlocal factory_calls
        factory_calls += 1
        assert environment == selected
        return client

    result = await run_read_only_validation(
        environment=environment(tmp_path, selected), client_factory=factory,
        ingestion_runner=fake_ingestion,
        db_path=str(tmp_path / f"{selected}.db"),
    )
    assert result["environment"] == selected
    assert result["balance"] == 12345
    assert result["position_count"] == 0
    assert result["order_count"] == 0
    assert result["fill_count"] == 0
    assert result["market_count"] == 1
    assert result["reconciliation"].status == "completed"
    assert result["alert_count"] == 0
    assert factory_calls == 1
    assert client.calls == {"balance": 1, "positions": 2, "orders": 2, "fills": 2}
    assert client.closed is True


async def test_exactly_one_ingestion_and_reconciliation_run(tmp_path, monkeypatch):
    from src.orders.reconciler import OrderReconciler

    client = AccountClient("production")
    counts = {"ingestion": 0, "reconciliation": 0}
    original_reconcile = OrderReconciler.reconcile

    async def counted_ingestion(manager, queue, kalshi_client):
        counts["ingestion"] += 1
        await fake_ingestion(manager, queue, kalshi_client)

    async def counted_reconcile(self, *args, **kwargs):
        counts["reconciliation"] += 1
        return await original_reconcile(self, *args, **kwargs)

    monkeypatch.setattr(OrderReconciler, "reconcile", counted_reconcile)
    await run_read_only_validation(
        environment=environment(tmp_path, "production"),
        client_factory=lambda environment: client,
        ingestion_runner=counted_ingestion,
        db_path=str(tmp_path / "bounded.db"),
    )
    assert counts == {"ingestion": 1, "reconciliation": 1}


async def test_resources_close_after_failure_and_secret_is_not_printed(tmp_path, capsys):
    secret = "SECRET_AUTH_EXCEPTION"

    class FailingClient(AccountClient):
        async def get_balance(self):
            raise RuntimeError(secret)

    client = FailingClient("production")
    with pytest.raises(ReadOnlyValidationError, match="authenticated account read failed"):
        await run_read_only_validation(
            environment=environment(tmp_path, "production"),
            client_factory=lambda environment: client,
            ingestion_runner=fake_ingestion,
            db_path=str(tmp_path / "failed.db"),
        )
    assert client.closed is True
    assert secret not in capsys.readouterr().out


async def test_missing_credentials_fail_before_client_construction(tmp_path):
    called = False

    def factory(**kwargs):
        nonlocal called
        called = True
        return AccountClient()

    values = environment(tmp_path)
    values["KALSHI_API_KEY"] = ""
    with pytest.raises(ReadOnlyValidationError, match="incomplete"):
        await run_read_only_validation(
            environment=values, client_factory=factory,
            db_path=str(tmp_path / "missing.db"),
        )
    assert called is False


@pytest.mark.parametrize("failure", [asyncio.TimeoutError(), KeyboardInterrupt()])
async def test_timeout_and_interruption_close_client_and_remove_temp_db(
    tmp_path, monkeypatch, failure,
):
    temporary_db = tmp_path / "temporary-validation.db"

    def mkstemp(**kwargs):
        import os
        descriptor = os.open(temporary_db, os.O_CREAT | os.O_RDWR)
        return descriptor, str(temporary_db)

    class InterruptedClient(AccountClient):
        async def get_balance(self):
            raise failure

    monkeypatch.setattr("scripts.read_only_validate.tempfile.mkstemp", mkstemp)
    client = InterruptedClient("production")
    expected = BaseException if isinstance(failure, KeyboardInterrupt) else ReadOnlyValidationError
    with pytest.raises(expected):
        await run_read_only_validation(
            environment=environment(tmp_path, "production"),
            client_factory=lambda environment: client,
            ingestion_runner=fake_ingestion,
        )
    assert client.closed is True
    assert not temporary_db.exists()


async def test_default_temp_database_does_not_touch_normal_database(tmp_path, monkeypatch):
    normal_db = tmp_path / "kalshi_trading.db"
    original = b"normal-database-sentinel"
    normal_db.write_bytes(original)
    temporary_db = tmp_path / "isolated-validation.db"

    def mkstemp(**kwargs):
        import os
        descriptor = os.open(temporary_db, os.O_CREAT | os.O_RDWR)
        return descriptor, str(temporary_db)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("scripts.read_only_validate.tempfile.mkstemp", mkstemp)
    client = AccountClient("production")
    await run_read_only_validation(
        environment=environment(tmp_path, "production"),
        client_factory=lambda environment: client,
        ingestion_runner=fake_ingestion,
    )
    assert normal_db.read_bytes() == original
    assert not temporary_db.exists()


async def test_malformed_balance_cannot_serialize_secret(tmp_path, capsys):
    secret = "SECRET_IN_MALFORMED_BALANCE"

    class MalformedClient(AccountClient):
        async def get_balance(self):
            return {"balance": secret, "signed_headers": secret}

    result = await run_read_only_validation(
        environment=environment(tmp_path, "production"),
        client_factory=lambda environment: MalformedClient("production"),
        ingestion_runner=fake_ingestion,
        db_path=str(tmp_path / "malformed.db"),
    )
    assert result["balance"] is None
    assert secret not in repr(result)
    assert secret not in capsys.readouterr().out

from datetime import datetime, timedelta

import pytest

from scripts.demo_shadow_validate import (
    DEMO_URL, DemoReadError, ReadOnlyDemoClient, run_demo_shadow_validation,
    validate_demo_safety,
)
from src.utils.database import Market


pytestmark = pytest.mark.asyncio


def environment(tmp_path, **overrides):
    key = tmp_path / "demo.pem"
    key.write_text("mock-key", encoding="utf-8")
    values = {
        "KALSHI_ENVIRONMENT": "demo",
        "KALSHI_API_KEY": "SECRET_DEMO_KEY",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "false",
        "LIVE_ORDER_SUBMISSION_KILL_SWITCH": "true",
    }
    values.update(overrides)
    return values


class ShadowClient:
    environment = "demo"
    base_url = DEMO_URL

    def __init__(self):
        self.closed = False
        self.submissions = 0
        self.cancellations = 0

    async def get_all_orders(self, **filters):
        return []

    async def get_all_fills(self, **filters):
        return []

    async def get_positions(self):
        return {"market_positions": []}

    async def place_order(self, **kwargs):
        self.submissions += 1
        raise AssertionError("demo shadow submitted an order")

    async def cancel_order(self, *args, **kwargs):
        self.cancellations += 1
        raise AssertionError("demo shadow canceled an order")

    async def close(self):
        self.closed = True


async def fake_ingestion(manager, queue, kalshi_client):
    assert isinstance(kalshi_client, ReadOnlyDemoClient)
    assert not hasattr(kalshi_client, "place_order")
    assert not hasattr(kalshi_client, "cancel_order")
    await manager.upsert_markets([Market(
        market_id="DEMO-MKT", title="Demo", yes_price=.4, no_price=.6,
        volume=100, expiration_ts=int((datetime.now() + timedelta(days=1)).timestamp()),
        category="demo", status="active", last_updated=datetime.now(),
    )])


async def test_demo_shadow_refuses_production_before_client_creation(tmp_path):
    called = False

    def factory():
        nonlocal called
        called = True
        return ShadowClient()

    with pytest.raises(RuntimeError, match="exactly demo"):
        await run_demo_shadow_validation(
            environment=environment(tmp_path, KALSHI_ENVIRONMENT="production"),
            client_factory=factory,
            db_path=str(tmp_path / "refused.db"),
        )
    assert called is False


async def test_demo_shadow_never_submits_cancels_or_leaks_secrets(tmp_path, capsys):
    client = ShadowClient()
    secret = "SECRET_DEMO_KEY"
    result = await run_demo_shadow_validation(
        environment=environment(tmp_path, KALSHI_API_KEY=secret),
        client_factory=lambda: client,
        ingestion_runner=fake_ingestion,
        db_path=str(tmp_path / "shadow.db"),
    )
    assert result["endpoint"] == DEMO_URL
    assert result["market_count"] == 1
    assert result["reconciliation"].status == "completed"
    assert client.submissions == 0
    assert client.cancellations == 0
    assert client.closed is True
    assert secret not in capsys.readouterr().out


async def test_demo_shadow_requires_safe_submission_flags(tmp_path):
    with pytest.raises(RuntimeError, match="must remain disabled"):
        validate_demo_safety(environment(
            tmp_path, AUTHORITATIVE_LIVE_EXECUTION_ENABLED="true"
        ))
    with pytest.raises(RuntimeError, match="must remain enabled"):
        validate_demo_safety(environment(
            tmp_path, LIVE_ORDER_SUBMISSION_KILL_SWITCH="false"
        ))


async def test_missing_credentials_fail_before_client_construction(tmp_path):
    called = False

    def factory():
        nonlocal called
        called = True
        return ShadowClient()

    missing_key = environment(tmp_path)
    missing_key["KALSHI_API_KEY"] = ""
    with pytest.raises(RuntimeError, match="incomplete"):
        await run_demo_shadow_validation(
            environment=missing_key, client_factory=factory,
            db_path=str(tmp_path / "missing-key.db"),
        )
    missing_file = environment(tmp_path)
    missing_file["KALSHI_PRIVATE_KEY_PATH"] = str(tmp_path / "absent.pem")
    with pytest.raises(RuntimeError, match="incomplete"):
        await run_demo_shadow_validation(
            environment=missing_file, client_factory=factory,
            db_path=str(tmp_path / "missing-file.db"),
        )
    assert called is False


async def test_read_only_facade_rejects_write_methods_without_delegating():
    class Client:
        write_calls = 0

        async def _make_authenticated_request(self, *args, **kwargs):
            self.write_calls += 1

    client = Client()
    facade = ReadOnlyDemoClient(client)
    assert not hasattr(facade, "place_order")
    assert not hasattr(facade, "cancel_order")
    with pytest.raises(DemoReadError, match="GET requests only"):
        await facade._make_authenticated_request("POST", "/trade-api/v2/orders")
    assert client.write_calls == 0


async def test_default_temporary_database_and_client_are_cleaned(tmp_path, monkeypatch):
    temporary_db = tmp_path / "bounded.db"

    def mkstemp(**kwargs):
        import os
        descriptor = os.open(temporary_db, os.O_CREAT | os.O_RDWR)
        return descriptor, str(temporary_db)

    monkeypatch.setattr("scripts.demo_shadow_validate.tempfile.mkstemp", mkstemp)
    client = ShadowClient()
    result = await run_demo_shadow_validation(
        environment=environment(tmp_path), client_factory=lambda: client,
        ingestion_runner=fake_ingestion,
    )
    assert result["reconciliation"].status == "completed"
    assert client.closed is True
    assert not temporary_db.exists()


async def test_underlying_read_exception_is_sanitized(tmp_path, capsys):
    secret = "SECRET_API_RESPONSE"

    class FailingClient(ShadowClient):
        async def get_all_orders(self, **filters):
            raise RuntimeError(secret)

    client = FailingClient()
    result = await run_demo_shadow_validation(
        environment=environment(tmp_path), client_factory=lambda: client,
        ingestion_runner=fake_ingestion,
        db_path=str(tmp_path / "failed-read.db"),
    )
    assert result["reconciliation"].status == "failed"
    assert secret not in capsys.readouterr().out
    assert client.closed is True

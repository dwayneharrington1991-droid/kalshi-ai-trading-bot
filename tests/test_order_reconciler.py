from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite
import pytest

from src.orders.exchange_models import ExchangeFill, ExchangeOrder
from src.orders.models import Order
from src.orders.reconciler import OrderReconciler
from src.orders.repository import OrderRepository, ReconciliationRunInProgress
from src.utils.database import DatabaseManager, Position


pytestmark = pytest.mark.asyncio


class FakeKalshiClient:
    def __init__(self, orders=None, fills=None, positions=None, failure=None):
        self.orders = orders or []
        self.fills = fills or []
        self.positions = positions or []
        self.failure = failure
        self.calls = []

    async def get_all_orders(self, **filters):
        self.calls.append(("orders", filters))
        if self.failure:
            raise self.failure
        return list(self.orders)

    async def get_all_fills(self, **filters):
        self.calls.append(("fills", filters))
        if self.failure:
            raise self.failure
        return list(self.fills)

    async def get_positions(self):
        self.calls.append(("positions", {}))
        if self.failure:
            raise self.failure
        return {"market_positions": list(self.positions)}


def exchange_order(
    order_id="exchange-1", client_id="client-1", status="resting",
    filled="0", remaining="10", expiration_at=None,
):
    return ExchangeOrder(
        order_id, client_id, "MARKET-1", "YES", "buy", status,
        Decimal("10"), Decimal(filled), Decimal(remaining), Decimal("0.42"),
        Decimal("0"), datetime.now(timezone.utc), datetime.now(timezone.utc),
        expiration_at, {"order_id": order_id, "status": status},
    )


def exchange_fill(fill_id, quantity, price, *, order_id="exchange-1", seconds=0):
    return ExchangeFill(
        fill_id, f"trade-{fill_id}", order_id, "MARKET-1", "YES", "buy",
        Decimal(str(quantity)), Decimal(str(price)), Decimal("0.01"), True,
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        {"fill_id": fill_id},
    )


@pytest.fixture
async def repository(tmp_path):
    db_path = str(tmp_path / "reconcile.db")
    await DatabaseManager(db_path).initialize()
    return OrderRepository(db_path)


async def local_submitted(repository, *, client_id="client-1", exchange_id=None):
    order_id = await repository.create_order(Order(
        client_order_id=client_id, market_id="MARKET-1", side="YES", action="buy",
        order_type="limit", limit_price=0.42, requested_quantity=10,
        submission_fingerprint=f"intent-{client_id}", environment="demo",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    await repository.transition_state(order_id, "submitted", "test")
    if exchange_id:
        await repository.store_exchange_id(order_id, exchange_id)
        await repository.transition_state(order_id, "accepted", "test")
    return order_id


async def test_partial_fills_out_of_order_and_restart_are_idempotent(repository):
    order_id = await local_submitted(repository, exchange_id="exchange-1")
    fills = [exchange_fill("fill-2", 2, 0.44, seconds=2), exchange_fill("fill-1", 3, 0.42)]
    client = FakeKalshiClient(
        [exchange_order(filled="5", remaining="5")], fills,
    )
    reconciler = OrderReconciler(repository, client, max_staleness_seconds=3600)
    first = await reconciler.reconcile(full=False)
    async with aiosqlite.connect(repository.db_path) as db:
        events_before = (await (await db.execute(
            "SELECT COUNT(*) FROM order_state_events"
        )).fetchone())[0]
    second = await reconciler.reconcile(trigger="restart", full=False)
    order = await repository.get_order(order_id)
    assert first.status == second.status == "completed"
    assert order["state"] == "partially_filled"
    assert order["filled_quantity"] == 5
    assert order["remaining_quantity"] == 5
    assert order["vwap_fill_price"] == pytest.approx(0.428)
    assert order["fees"] == pytest.approx(0.02)
    async with aiosqlite.connect(repository.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM order_fills")).fetchone())[0]
        events_after = (await (await db.execute(
            "SELECT COUNT(*) FROM order_state_events"
        )).fetchone())[0]
    assert count == 2
    assert events_after == events_before


async def test_canceled_remainder_keeps_partial_fill_totals(repository):
    order_id = await local_submitted(repository, exchange_id="exchange-1")
    client = FakeKalshiClient(
        [exchange_order(status="canceled", filled="3", remaining="7")],
        [exchange_fill("fill-1", 3, 0.41)],
    )
    result = await OrderReconciler(repository, client, max_staleness_seconds=3600).reconcile(full=False)
    order = await repository.get_order(order_id)
    assert result.status == "completed"
    assert order["state"] == "canceled"
    assert order["filled_quantity"] == 3
    assert order["remaining_quantity"] == 7
    assert order["vwap_fill_price"] == pytest.approx(0.41)


async def test_terminal_exchange_status_with_missing_fills_fails_closed(repository):
    order_id = await local_submitted(repository, exchange_id="exchange-1")
    client = FakeKalshiClient(
        [exchange_order(status="executed", filled="10", remaining="0")], [],
    )
    result = await OrderReconciler(
        repository, client, max_staleness_seconds=3600,
    ).reconcile(full=False)
    assert result.status == "completed_with_mismatches"
    order = await repository.get_order(order_id)
    assert order["state"] == "verification_failed"
    assert order["filled_quantity"] == 0


async def test_matches_by_client_id_and_persists_exchange_id(repository):
    order_id = await local_submitted(repository)
    client = FakeKalshiClient([exchange_order()], [])
    await OrderReconciler(repository, client, max_staleness_seconds=3600).reconcile(full=False)
    order = await repository.get_order(order_id)
    assert order["exchange_order_id"] == "exchange-1"
    assert order["state"] == "resting"


async def test_ambiguous_and_missing_orders_fail_closed(repository):
    ambiguous_id = await local_submitted(repository, client_id="ambiguous")
    client = FakeKalshiClient([
        exchange_order("one", "ambiguous"), exchange_order("two", "ambiguous"),
    ])
    result = await OrderReconciler(
        repository, client, verification_timeout_seconds=0, max_staleness_seconds=0,
    ).reconcile(full=False)
    assert result.mismatch_count >= 1
    assert (await repository.get_order(ambiguous_id))["state"] == "verification_failed"

    missing_id = await local_submitted(repository, client_id="missing")
    await OrderReconciler(
        repository, FakeKalshiClient(), verification_timeout_seconds=0,
        max_staleness_seconds=3600,
    ).reconcile(full=False)
    assert (await repository.get_order(missing_id))["state"] == "verification_failed"


async def test_remote_only_order_records_alert(repository):
    result = await OrderReconciler(
        repository, FakeKalshiClient([exchange_order()], []),
    ).reconcile(full=True)
    assert result.status == "completed_with_mismatches"
    async with aiosqlite.connect(repository.db_path) as db:
        kinds = {row[0] for row in await (await db.execute(
            "SELECT kind FROM reconciliation_alerts"
        )).fetchall()}
    assert "remote_only_order" in kinds


async def test_api_failure_records_run_and_quarantines_order(repository):
    order_id = await local_submitted(repository)
    result = await OrderReconciler(
        repository, FakeKalshiClient(failure=RuntimeError("offline")),
    ).reconcile()
    assert result.status == "failed"
    assert (await repository.get_order(order_id))["state"] == "verification_failed"
    async with aiosqlite.connect(repository.db_path) as db:
        run = await (await db.execute(
            "SELECT status, error_count FROM reconciliation_runs WHERE id = ?", (result.run_id,)
        )).fetchone()
    assert run == ("failed", 1)
    async with aiosqlite.connect(repository.db_path) as db:
        kinds = {row[0] for row in await (await db.execute(
            "SELECT kind FROM reconciliation_alerts"
        )).fetchall()}
    assert "reconciliation_api_failure" in kinds
    assert "local_only_order" not in kinds


async def test_shadow_mode_detects_position_mismatch_without_mutating_position(repository):
    manager = DatabaseManager(repository.db_path)
    position_id = await manager.add_position(Position(
        market_id="MARKET-1", side="YES", entry_price=0.4, quantity=4,
        timestamp=datetime.now(), live=True, status="open",
    ))
    async with aiosqlite.connect(repository.db_path) as db:
        before = await (await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))).fetchone()
    result = await OrderReconciler(
        repository, FakeKalshiClient(positions=[{"ticker": "MARKET-1", "position_fp": "3"}]),
    ).reconcile(full=True)
    async with aiosqlite.connect(repository.db_path) as db:
        after = await (await db.execute("SELECT * FROM positions WHERE id = ?", (position_id,))).fetchone()
    assert result.mismatch_count == 1
    assert after == before


async def test_verified_manual_exchange_position_reconciles_without_bot_fill(repository):
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute("""
            INSERT INTO external_account_positions
            (market_id, side, quantity, source, exchange_order_id, exchange_fill_id,
             first_observed_at, last_verified_at, status, metadata_json)
            VALUES ('MANUAL', 'YES', 1.67, 'manual_exchange_activity', 'order-x', 'fill-x',
                    'now', 'now', 'active', '{"bot_fill": false, "pnl": null}')
        """)
        await db.commit()
    result = await OrderReconciler(
        repository, FakeKalshiClient(positions=[{"ticker": "MANUAL", "position_fp": "1.67"}]),
    ).reconcile(full=True)
    assert result.mismatch_count == 0
    async with aiosqlite.connect(repository.db_path) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM order_fills")).fetchone())[0] == 0
        assert (await (await db.execute("SELECT COUNT(*) FROM trade_logs")).fetchone())[0] == 0


async def test_paper_mode_never_calls_exchange_or_writes_run(repository):
    client = FakeKalshiClient(failure=AssertionError("exchange must not be called"))
    result = await OrderReconciler(repository, client, paper_mode=True).reconcile()
    assert result.status == "skipped_paper"
    assert client.calls == []
    async with aiosqlite.connect(repository.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM reconciliation_runs")).fetchone())[0]
    assert count == 0


async def test_non_shadow_mode_is_rejected(repository):
    with pytest.raises(ValueError):
        OrderReconciler(repository, FakeKalshiClient(), shadow_mode=False)


async def test_authoritative_not_found_is_distinct_from_api_failure(repository):
    await local_submitted(repository, client_id="missing")
    result = await OrderReconciler(
        repository, FakeKalshiClient(), verification_timeout_seconds=0,
        max_staleness_seconds=3600,
    ).reconcile(full=False)
    assert result.status == "completed_with_mismatches"
    async with aiosqlite.connect(repository.db_path) as db:
        kinds = {row[0] for row in await (await db.execute(
            "SELECT kind FROM reconciliation_alerts"
        )).fetchall()}
    assert "local_only_order" in kinds
    assert "reconciliation_api_failure" not in kinds


async def test_concurrent_reconciliation_run_is_rejected_transactionally(repository):
    first_run = await repository.start_reconciliation_run("test")
    with pytest.raises(ReconciliationRunInProgress):
        await repository.start_reconciliation_run("overlap")
    await repository.finish_reconciliation_run(first_run, "completed")
    second_run = await repository.start_reconciliation_run("after")
    assert second_run != first_run


async def test_startup_failure_is_durable_and_does_not_mutate_positions(repository):
    manager = DatabaseManager(repository.db_path)
    position_id = await manager.add_position(Position(
        market_id="MARKET-1", side="YES", entry_price=0.4, quantity=4,
        timestamp=datetime.now(), live=True, status="open",
    ))
    async with aiosqlite.connect(repository.db_path) as db:
        before = await (await db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )).fetchone()
    result = await OrderReconciler(
        repository, FakeKalshiClient(failure=TimeoutError("timeout")),
    ).reconcile(trigger="startup")
    async with aiosqlite.connect(repository.db_path) as db:
        after = await (await db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )).fetchone()
    assert result.status == "failed"
    assert after == before

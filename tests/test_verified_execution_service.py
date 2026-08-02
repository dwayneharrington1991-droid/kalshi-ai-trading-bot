from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite
import pytest

from src.config.settings import TradingConfig
from src.orders.exchange_models import ExchangeFill, ExchangeOrder
from src.orders.execution_service import (
    ExecutionSafetyConfig, ExecutionSafetyError, OrderIntent, VerifiedExecutionService,
)
from src.orders.reconciler import OrderReconciler
from src.orders.repository import OrderRepository
from src.orders.models import Order, OrderFill
from src.utils.database import DatabaseManager, Position
from src.jobs.execute import execute_position

pytestmark = pytest.mark.asyncio


class FakeClient:
    environment = "demo"

    def __init__(self, status="resting", filled=0, fill_prices=None, place_error=None,
                 malformed=False):
        self.status = status
        self.filled = Decimal(str(filled))
        self.fill_prices = fill_prices or []
        self.place_error = place_error
        self.malformed = malformed
        self.place_calls = 0
        self.client_order_id = None
        self.exchange_order_id = "exchange-1"
        self.fill_time = datetime.now(timezone.utc)

    async def get_balance(self):
        return {"balance": 100000}

    async def place_order(self, **params):
        self.place_calls += 1
        self.client_order_id = params["client_order_id"]
        if self.place_error:
            raise self.place_error
        if self.malformed:
            return {"order": {"status": "accepted"}}
        return {"order": {"order_id": self.exchange_order_id, "status": "accepted"}}

    async def get_all_orders(self, **filters):
        if not self.client_order_id:
            return []
        return [ExchangeOrder(
            self.exchange_order_id, self.client_order_id, "MKT", "YES", "buy",
            self.status, Decimal("10"), self.filled, Decimal("10") - self.filled,
            Decimal("0.40"), Decimal("0"), datetime.now(timezone.utc),
            datetime.now(timezone.utc), None, {"status": self.status},
        )]

    async def get_all_fills(self, **filters):
        return [ExchangeFill(
            f"fill-{index}", f"trade-{index}", self.exchange_order_id, "MKT", "YES",
            "buy", Decimal(str(quantity)), Decimal(str(price)), Decimal("0.01"), True,
            self.fill_time, {"fill": index},
        ) for index, (quantity, price) in enumerate(self.fill_prices)]

    async def get_positions(self):
        raise AssertionError("full position reconciliation is not used post-submission")


@pytest.fixture
async def setup(tmp_path):
    db_path = str(tmp_path / "execution.db")
    manager = DatabaseManager(db_path)
    await manager.initialize()
    repository = OrderRepository(db_path)
    run_id = await repository.start_reconciliation_run("health")
    await repository.finish_reconciliation_run(run_id, "completed")
    position_id = await manager.add_position(Position(
        market_id="MKT", side="YES", entry_price=0.4, quantity=10,
        timestamp=datetime.now(), live=False, status="pending",
    ))
    return manager, repository, position_id


def intent(position_id, action="buy", quantity=10):
    return OrderIntent("MKT", "YES", action, quantity, 0.4, "limit",
                       position_id, "demo")


def service(repository, client, **overrides):
    values = dict(live_mode=True, authoritative_execution_enabled=True,
                  reconciliation_enabled=True, kill_switch=False,
                  reconciliation_max_age_seconds=60,
                  allow_risk_reducing_exits=True)
    values.update(overrides)
    return VerifiedExecutionService(
        repository, client, OrderReconciler(repository, client, max_staleness_seconds=60),
        ExecutionSafetyConfig(**values),
    )


async def position_row(repository, position_id):
    async with aiosqlite.connect(repository.db_path) as db:
        db.row_factory = aiosqlite.Row
        return dict(await (await db.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        )).fetchone())


@pytest.mark.parametrize("status", ["accepted", "resting"])
async def test_acceptance_without_fills_does_not_open_position(setup, status):
    _, repository, position_id = setup
    client = FakeClient(status=status)
    before = await position_row(repository, position_id)
    await service(repository, client).execute(intent(position_id))
    assert await position_row(repository, position_id) == before


@pytest.mark.parametrize("status,filled,fills,expected_state", [
    ("executed", 10, [(10, .43)], "fully_filled"),
    ("resting", 4, [(4, .42)], "partially_filled"),
    ("canceled", 4, [(4, .42)], "canceled"),
])
async def test_buy_projection_uses_only_authoritative_fills(
    setup, status, filled, fills, expected_state,
):
    _, repository, position_id = setup
    result = await service(repository, FakeClient(status, filled, fills)).execute(
        intent(position_id)
    )
    position = await position_row(repository, position_id)
    assert result.state == expected_state
    assert position["quantity"] == filled
    assert position["live"] == 1
    assert position["average_entry_price"] == pytest.approx(fills[0][1])


async def make_live_position(setup):
    _, repository, position_id = setup
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute("""
            UPDATE positions SET live=1, status='open', open_quantity=10,
                filled_quantity=10 WHERE id=?
        """, (position_id,))
        await db.commit()
    return repository, position_id


@pytest.mark.parametrize("filled,status,expected", [
    (0, "resting", 10), (4, "resting", 6), (10, "executed", 0),
])
async def test_sell_acceptance_and_fills_reduce_only_authoritative_quantity(
    setup, filled, status, expected,
):
    repository, position_id = await make_live_position(setup)
    client = FakeClient(status, filled, [(filled, .55)] if filled else [])
    original_get_fills = client.get_all_fills

    async def sell_fills(**filters):
        fills = await original_get_fills(**filters)
        return [ExchangeFill(f.fill_id, f.trade_id, f.order_id, f.market_id, f.side,
                             "sell", f.quantity, f.price, f.fee, f.is_taker,
                             f.filled_at, f.raw) for f in fills]
    client.get_all_fills = sell_fills
    original_get_orders = client.get_all_orders

    async def sell_orders(**filters):
        orders = await original_get_orders(**filters)
        return [ExchangeOrder(o.order_id, o.client_order_id, o.market_id, o.side, "sell",
                              o.status, o.initial_quantity, o.filled_quantity,
                              o.remaining_quantity, o.price, o.fees, o.created_at,
                              o.updated_at, o.expiration_at, o.raw) for o in orders]
    client.get_all_orders = sell_orders
    await service(repository, client).execute(intent(position_id, "sell"))
    position = await position_row(repository, position_id)
    assert position["quantity"] == expected
    assert position["status"] == ("closed" if expected == 0 else "open")


@pytest.mark.parametrize("error,malformed", [(TimeoutError("timeout"), False), (None, True)])
async def test_unverifiable_submission_fails_closed(setup, error, malformed):
    _, repository, position_id = setup
    client = FakeClient(place_error=error, malformed=malformed)
    result = await service(repository, client).execute(intent(position_id))
    assert result.state == "verification_failed"
    assert (await position_row(repository, position_id))["live"] == 0


async def test_timeout_after_acceptance_recovers_same_client_id_without_resubmit(setup):
    _, repository, position_id = setup
    client = FakeClient("executed", 10, [(10, .41)], place_error=TimeoutError())
    first = await service(repository, client).execute(intent(position_id))
    client.place_error = None
    second = await service(repository, client).execute(intent(position_id))
    assert first.client_order_id == second.client_order_id
    assert client.place_calls == 1
    assert second.recovered is True
    assert (await position_row(repository, position_id))["quantity"] == 10


async def test_duplicate_and_fill_replay_are_idempotent(setup):
    _, repository, position_id = setup
    client = FakeClient("executed", 10, [(10, .41)])
    first = await service(repository, client).execute(intent(position_id))
    second = await service(repository, client).execute(intent(position_id))
    assert first.client_order_id == second.client_order_id
    assert client.place_calls == 1
    async with aiosqlite.connect(repository.db_path) as db:
        fills = (await (await db.execute("SELECT COUNT(*) FROM order_fills")).fetchone())[0]
        projections = (await (await db.execute(
            "SELECT COUNT(*) FROM position_fill_projections"
        )).fetchone())[0]
    assert fills == projections == 1


@pytest.mark.parametrize("override", [
    {"authoritative_execution_enabled": False}, {"kill_switch": True},
    {"reconciliation_enabled": False},
])
async def test_safety_gates_block_before_submission(setup, override):
    _, repository, position_id = setup
    client = FakeClient()
    with pytest.raises(ExecutionSafetyError):
        await service(repository, client, **override).execute(intent(position_id))
    assert client.place_calls == 0


async def test_stale_or_critical_reconciliation_blocks_submission(setup):
    _, repository, position_id = setup
    await repository.record_alert("critical", "test_critical")
    client = FakeClient()
    with pytest.raises(ExecutionSafetyError):
        await service(repository, client).execute(intent(position_id))
    assert client.place_calls == 0


async def test_stale_reconciliation_blocks_submission(setup):
    _, repository, position_id = setup
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute(
            "UPDATE reconciliation_runs SET completed_at='2000-01-01T00:00:00+00:00'"
        )
        await db.commit()
    client = FakeClient()
    with pytest.raises(ExecutionSafetyError, match="stale"):
        await service(repository, client).execute(intent(position_id))
    assert client.place_calls == 0


async def test_stale_reconciliation_can_recover_after_fresh_health_checkpoint(setup):
    _, repository, position_id = setup
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute(
            "UPDATE reconciliation_runs SET completed_at='2000-01-01T00:00:00+00:00'"
        )
        await db.commit()
    client = FakeClient()
    with pytest.raises(ExecutionSafetyError, match="stale"):
        await service(repository, client).execute(intent(position_id))
    run_id = await repository.start_reconciliation_run("fresh")
    await repository.finish_reconciliation_run(run_id, "completed")
    result = await service(repository, client).execute(intent(position_id))
    assert result.state == "resting"
    assert client.place_calls == 1


async def test_paper_execution_uses_existing_simulation_without_exchange_calls(setup):
    manager, repository, position_id = setup
    position = Position("MKT", "YES", .4, 10, datetime.now(), id=position_id)

    class NoNetworkClient:
        def __getattr__(self, name):
            raise AssertionError(f"paper mode accessed exchange method {name}")

    assert await execute_position(position, False, manager, NoNetworkClient()) is True
    assert (await position_row(repository, position_id))["live"] == 1


async def test_authoritative_missing_order_after_acceptance_is_quarantined(setup):
    _, repository, position_id = setup
    client = FakeClient()

    async def no_orders(**filters):
        return []
    client.get_all_orders = no_orders
    result = await service(repository, client).execute(intent(position_id))
    assert result.state == "verification_failed"
    assert (await position_row(repository, position_id))["live"] == 0


async def test_crash_after_http_before_exchange_id_persistence_recovers_without_resubmit(
    setup, monkeypatch,
):
    _, repository, position_id = setup
    client = FakeClient("executed", 10, [(10, .41)])
    original = repository.store_exchange_id

    async def crash(*args, **kwargs):
        raise KeyboardInterrupt("simulated process crash")
    monkeypatch.setattr(repository, "store_exchange_id", crash)
    with pytest.raises(KeyboardInterrupt):
        await service(repository, client).execute(intent(position_id))
    monkeypatch.setattr(repository, "store_exchange_id", original)
    result = await service(repository, client).execute(intent(position_id))
    assert result.recovered is True
    assert result.state == "fully_filled"
    assert client.place_calls == 1


async def test_concurrent_duplicate_intents_submit_at_most_once(setup):
    import asyncio

    _, repository, position_id = setup
    client = FakeClient()
    executor = service(repository, client)
    results = await asyncio.gather(
        executor.execute(intent(position_id)), executor.execute(intent(position_id)),
        return_exceptions=True,
    )
    assert client.place_calls == 1
    async with aiosqlite.connect(repository.db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM orders")).fetchone())[0]
    assert count == 1
    assert any(not isinstance(item, BaseException) for item in results)


async def test_concurrent_projection_replay_does_not_double_apply(setup):
    import asyncio

    _, repository, position_id = setup
    client = FakeClient("executed", 10, [(10, .41)])
    await service(repository, client).execute(intent(position_id))
    await asyncio.gather(
        repository.project_authoritative_fills(position_id),
        repository.project_authoritative_fills(position_id),
    )
    position = await position_row(repository, position_id)
    assert position["quantity"] == 10
    async with aiosqlite.connect(repository.db_path) as db:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM position_fill_projections"
        )).fetchone())[0]
    assert count == 1


async def test_partial_buy_accepts_additional_fill_after_restart(setup):
    _, repository, position_id = setup
    client = FakeClient("resting", 4, [(4, .40)])
    executor = service(repository, client)
    await executor.execute(intent(position_id))
    client.status = "executed"
    client.filled = Decimal("10")
    client.fill_prices = [(4, .40), (6, .50)]
    result = await service(repository, client).execute(intent(position_id))
    position = await position_row(repository, position_id)
    assert result.state == "fully_filled"
    assert position["quantity"] == 10
    assert position["average_entry_price"] == pytest.approx(.46)
    assert client.place_calls == 1


async def test_partial_sell_then_cancellation_preserves_open_remainder(setup):
    repository, position_id = await make_live_position(setup)
    client = FakeClient("canceled", 4, [(4, .55)])
    original_orders, original_fills = client.get_all_orders, client.get_all_fills

    async def sell_orders(**kwargs):
        return [ExchangeOrder(o.order_id, o.client_order_id, o.market_id, o.side, "sell",
                              o.status, o.initial_quantity, o.filled_quantity,
                              o.remaining_quantity, o.price, o.fees, o.created_at,
                              o.updated_at, o.expiration_at, o.raw)
                for o in await original_orders(**kwargs)]

    async def sell_fills(**kwargs):
        return [ExchangeFill(f.fill_id, f.trade_id, f.order_id, f.market_id, f.side,
                             "sell", f.quantity, f.price, f.fee, f.is_taker,
                             f.filled_at, f.raw) for f in await original_fills(**kwargs)]
    client.get_all_orders, client.get_all_fills = sell_orders, sell_fills
    result = await service(repository, client).execute(intent(position_id, "sell"))
    assert result.state == "canceled"
    assert (await position_row(repository, position_id))["quantity"] == 6


async def test_sell_over_close_is_blocked_before_submission(setup):
    repository, position_id = await make_live_position(setup)
    client = FakeClient()
    with pytest.raises(ExecutionSafetyError, match="exceeds"):
        await service(repository, client).execute(intent(position_id, "sell", 11))
    assert client.place_calls == 0


async def test_conflicting_replayed_fill_payload_fails_closed(setup):
    _, repository, position_id = setup
    client = FakeClient("executed", 10, [(10, .41)])
    result = await service(repository, client).execute(intent(position_id))
    order = await repository.get_order(result.order_id)
    with pytest.raises(ValueError, match="conflicting payload"):
        await repository.insert_fill(result.order_id, OrderFill(
            exchange_fill_id="fill-0", exchange_order_id=order["exchange_order_id"],
            quantity=10, price=.42, fee=.01, filled_at=datetime.now(timezone.utc),
        ))


async def test_fill_and_overclose_projection_roll_back_together(setup):
    repository, position_id = await make_live_position(setup)
    async with aiosqlite.connect(repository.db_path) as db:
        await db.execute(
            "UPDATE positions SET quantity=5, open_quantity=5 WHERE id=?", (position_id,)
        )
        await db.commit()
    order_id = await repository.create_order(Order(
        client_order_id="overclose-client", market_id="MKT", side="YES", action="sell",
        order_type="limit", limit_price=.5, requested_quantity=10,
        submission_fingerprint="overclose-fingerprint", environment="demo",
        position_id=position_id,
    ))
    await repository.transition_state(order_id, "submitted", "test")
    await repository.store_exchange_id(order_id, "overclose-exchange")
    await repository.transition_state(order_id, "accepted", "test")
    with pytest.raises(ValueError, match="over-close"):
        await repository.insert_fill(order_id, OrderFill(
            exchange_fill_id="overclose-fill", exchange_order_id="overclose-exchange",
            quantity=10, price=.5, filled_at=datetime.now(timezone.utc),
        ), project_position=True)
    async with aiosqlite.connect(repository.db_path) as db:
        fill_count = (await (await db.execute(
            "SELECT COUNT(*) FROM order_fills WHERE order_id=?", (order_id,)
        )).fetchone())[0]
        projection_count = (await (await db.execute(
            "SELECT COUNT(*) FROM position_fill_projections WHERE order_id=?", (order_id,)
        )).fetchone())[0]
    assert fill_count == projection_count == 0
    assert (await position_row(repository, position_id))["quantity"] == 5


async def test_wrong_environment_and_missing_production_ack_block_submission(setup):
    _, repository, position_id = setup
    client = FakeClient()
    client.environment = "production"
    with pytest.raises(ExecutionSafetyError, match="environment"):
        await service(repository, client).execute(intent(position_id))

    production_intent = OrderIntent("MKT", "YES", "buy", 10, .4, "limit",
                                    position_id, "production")
    with pytest.raises(ExecutionSafetyError, match="acknowledgement"):
        await service(repository, client).execute(production_intent)
    assert client.place_calls == 0


async def test_authoritative_execution_defaults_are_fail_closed(monkeypatch):
    monkeypatch.delenv("AUTHORITATIVE_LIVE_EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("LIVE_ORDER_SUBMISSION_KILL_SWITCH", raising=False)
    config = TradingConfig()
    assert config.authoritative_live_execution_enabled is False
    assert config.live_order_submission_kill_switch is True

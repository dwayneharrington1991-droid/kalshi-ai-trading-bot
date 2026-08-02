from datetime import datetime

import aiosqlite
import pytest

from src.orders.models import Order, OrderFill
from src.orders.repository import OrderRepository
from src.orders.state_machine import InvalidOrderTransition
from src.utils.database import DatabaseManager


pytestmark = pytest.mark.asyncio


@pytest.fixture
async def order_repo(tmp_path):
    db_path = str(tmp_path / "orders.db")
    await DatabaseManager(db_path).initialize()
    return OrderRepository(db_path)


def make_order(client_id="client-1", fingerprint="intent-1"):
    return Order(
        client_order_id=client_id, market_id="TEST-MARKET", side="YES",
        action="buy", order_type="limit", limit_price=0.42,
        requested_quantity=10, submission_fingerprint=fingerprint,
        environment="demo", strategy="test",
    )


async def test_create_order_and_initial_event(order_repo):
    order_id = await order_repo.create_order(make_order())
    order = await order_repo.get_order(order_id)
    assert order["state"] == "locally_created"
    assert order["remaining_quantity"] == 10
    async with aiosqlite.connect(order_repo.db_path) as db:
        event = await (await db.execute(
            "SELECT from_state, to_state FROM order_state_events WHERE order_id = ?", (order_id,)
        )).fetchone()
    assert event == (None, "locally_created")


async def test_client_order_id_is_unique(order_repo):
    await order_repo.create_order(make_order())
    with pytest.raises(aiosqlite.IntegrityError):
        await order_repo.create_order(make_order(fingerprint="other-intent"))


async def test_exchange_id_and_state_history(order_repo):
    order_id = await order_repo.create_order(make_order())
    await order_repo.transition_state(order_id, "submitted", "unit-test")
    await order_repo.store_exchange_id(order_id, "exchange-123", "{}")
    await order_repo.transition_state(order_id, "accepted", "submit_response")
    order = await order_repo.get_order(order_id)
    assert order["exchange_order_id"] == "exchange-123"
    assert order["state"] == "accepted"
    assert order["filled_quantity"] == 0
    async with aiosqlite.connect(order_repo.db_path) as db:
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM order_state_events WHERE order_id = ?", (order_id,)
        )).fetchone())[0]
    assert count == 3


async def test_exchange_id_is_idempotent_but_immutable(order_repo):
    order_id = await order_repo.create_order(make_order())
    await order_repo.store_exchange_id(order_id, "exchange-123")
    await order_repo.store_exchange_id(order_id, "exchange-123")
    with pytest.raises(ValueError):
        await order_repo.store_exchange_id(order_id, "exchange-other")
    assert (await order_repo.get_order(order_id))["exchange_order_id"] == "exchange-123"


async def test_invalid_transition_is_atomic(order_repo):
    order_id = await order_repo.create_order(make_order())
    with pytest.raises(InvalidOrderTransition):
        await order_repo.transition_state(order_id, "fully_filled", "unit-test")
    assert (await order_repo.get_order(order_id))["state"] == "locally_created"


async def test_fill_insert_is_idempotent_and_calculates_vwap(order_repo):
    order_id = await order_repo.create_order(make_order())
    await order_repo.store_exchange_id(order_id, "exchange-123")
    fill1 = OrderFill("fill-1", "exchange-123", 3, 0.42, datetime(2026, 1, 1), fee=0.03)
    fill2 = OrderFill("fill-2", "exchange-123", 2, 0.44, datetime(2026, 1, 1), fee=0.02)
    assert await order_repo.insert_fill(order_id, fill1) is True
    assert await order_repo.insert_fill(order_id, fill1) is False
    assert await order_repo.insert_fill(order_id, fill2) is True
    order = await order_repo.get_order(order_id)
    assert order["filled_quantity"] == 5
    assert order["remaining_quantity"] == 5
    assert order["vwap_fill_price"] == pytest.approx(0.428)
    assert order["fees"] == pytest.approx(0.05)


@pytest.mark.parametrize("quantity,price,fee", [
    (0, 0.4, 0), (-1, 0.4, 0), (1, -0.1, 0), (1, 1.1, 0), (1, 0.4, -0.1),
])
async def test_invalid_fill_data_is_rejected(order_repo, quantity, price, fee):
    order_id = await order_repo.create_order(make_order())
    await order_repo.store_exchange_id(order_id, "exchange-123")
    fill = OrderFill("bad-fill", "exchange-123", quantity, price, datetime(2026, 1, 1), fee=fee)
    with pytest.raises(ValueError):
        await order_repo.insert_fill(order_id, fill)
    assert (await order_repo.get_order(order_id))["filled_quantity"] == 0


async def test_fill_cannot_be_attached_to_wrong_order(order_repo):
    first = await order_repo.create_order(make_order())
    second = await order_repo.create_order(make_order("client-2", "intent-2"))
    await order_repo.store_exchange_id(first, "exchange-1")
    await order_repo.store_exchange_id(second, "exchange-2")
    fill = OrderFill("fill-1", "exchange-1", 1, 0.4, datetime(2026, 1, 1))
    assert await order_repo.insert_fill(first, fill) is True
    with pytest.raises(ValueError):
        await order_repo.insert_fill(second, fill)


async def test_fill_requires_persisted_exchange_order_id(order_repo):
    order_id = await order_repo.create_order(make_order())
    fill = OrderFill("fill-1", "exchange-1", 1, 0.4, datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        await order_repo.insert_fill(order_id, fill)


async def test_overfill_is_rejected_without_changing_totals(order_repo):
    order_id = await order_repo.create_order(make_order())
    await order_repo.store_exchange_id(order_id, "exchange-123")
    fill = OrderFill("fill-too-large", "exchange-123", 11, 0.4, datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        await order_repo.insert_fill(order_id, fill)
    assert (await order_repo.get_order(order_id))["filled_quantity"] == 0


async def test_active_and_uncertain_query_excludes_terminal(order_repo):
    active_id = await order_repo.create_order(make_order())
    terminal_id = await order_repo.create_order(make_order("client-2", "intent-2"))
    await order_repo.transition_state(terminal_id, "submitted", "unit-test")
    await order_repo.transition_state(terminal_id, "rejected", "unit-test")
    rows = await order_repo.get_active_or_uncertain_orders()
    assert {row["id"] for row in rows} == {active_id}


async def test_uncertain_query_is_distinct_from_all_active_orders(order_repo):
    local_id = await order_repo.create_order(make_order())
    uncertain_id = await order_repo.create_order(make_order("client-2", "intent-2"))
    await order_repo.transition_state(uncertain_id, "submitted", "unit-test")
    uncertain = await order_repo.get_uncertain_orders()
    assert {row["id"] for row in uncertain} == {uncertain_id}
    assert {row["id"] for row in await order_repo.get_active_orders()} == {local_id, uncertain_id}

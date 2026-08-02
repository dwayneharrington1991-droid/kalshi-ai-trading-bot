from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.clients.kalshi_client import KalshiAPIError, KalshiClient
from src.orders.exchange_models import ExchangeFill, ExchangeOrder


pytestmark = pytest.mark.asyncio


def client_without_credentials():
    client = object.__new__(KalshiClient)
    client._make_authenticated_request = AsyncMock()
    return client


def raw_order(order_id="order-1", client_id="client-1", status="resting"):
    return {
        "order_id": order_id, "client_order_id": client_id, "ticker": "MARKET-1",
        "outcome_side": "yes", "action": "buy", "status": status,
        "initial_count_fp": "10.00", "fill_count_fp": "2.00",
        "remaining_count_fp": "8.00", "yes_price_dollars": "0.4200",
        "maker_fees_dollars": "0.01", "taker_fees_dollars": "0.02",
        "created_time": "2026-08-02T12:00:00Z",
    }


def raw_fill(fill_id="fill-1", order_id="order-1", count="2.00", price="0.4200"):
    return {
        "fill_id": fill_id, "trade_id": f"trade-{fill_id}", "order_id": order_id,
        "ticker": "MARKET-1", "outcome_side": "yes", "action": "buy",
        "count_fp": count, "yes_price_dollars": price, "fee_cost_dollars": "0.03",
        "created_time": "2026-08-02T12:01:00Z", "is_taker": True,
    }


async def test_get_order_uses_read_only_endpoint():
    client = client_without_credentials()
    client._make_authenticated_request.return_value = {"order": raw_order()}
    response = await client.get_order("order-1")
    assert response["order"]["order_id"] == "order-1"
    client._make_authenticated_request.assert_awaited_once_with(
        "GET", "/trade-api/v2/portfolio/orders/order-1"
    )


async def test_order_pagination_and_normalization():
    client = client_without_credentials()
    client._make_authenticated_request.side_effect = [
        {"orders": [raw_order("order-1")], "cursor": "next"},
        {"orders": [raw_order("order-2", "client-2")], "cursor": ""},
    ]
    orders = await client.get_all_orders(limit=1)
    assert [item.order_id for item in orders] == ["order-1", "order-2"]
    assert all(isinstance(item, ExchangeOrder) for item in orders)
    assert orders[0].filled_quantity == 2
    assert orders[0].price == Decimal("0.4200")
    second_params = client._make_authenticated_request.await_args_list[1].kwargs["params"]
    assert second_params["cursor"] == "next"


async def test_fill_pagination_and_order_filter():
    client = client_without_credentials()
    client._make_authenticated_request.side_effect = [
        {"fills": [raw_fill("fill-1")], "cursor": "next"},
        {"fills": [raw_fill("fill-2")], "cursor": None},
    ]
    fills = await client.get_all_fills(order_id="order-1", limit=1)
    assert [item.fill_id for item in fills] == ["fill-1", "fill-2"]
    assert all(isinstance(item, ExchangeFill) for item in fills)
    assert fills[0].fee == Decimal("0.03")
    first_params = client._make_authenticated_request.await_args_list[0].kwargs["params"]
    assert first_params["order_id"] == "order-1"


async def test_legacy_cent_fee_is_converted_to_dollars():
    client = client_without_credentials()
    fill = raw_fill()
    fill.pop("fee_cost_dollars")
    fill["fee_cost"] = 3
    client._make_authenticated_request.return_value = {
        "fills": [fill], "cursor": None,
    }
    fills = await client.get_all_fills()
    assert fills[0].fee == Decimal("0.03")


async def test_client_order_id_filter_is_local_not_sent_to_kalshi():
    client = client_without_credentials()
    client._make_authenticated_request.return_value = {
        "orders": [raw_order("one", "wanted"), raw_order("two", "other")], "cursor": None,
    }
    response = await client.get_orders(client_order_id="wanted")
    assert [item["order_id"] for item in response["orders"]] == ["one"]
    params = client._make_authenticated_request.await_args.kwargs["params"]
    assert "client_order_id" not in params


async def test_explicit_environment_urls(monkeypatch):
    monkeypatch.setattr(KalshiClient, "_load_private_key", lambda self: None)
    demo = KalshiClient(api_key="fake", environment="demo", private_key_path="unused")
    production = KalshiClient(api_key="fake", environment="production", private_key_path="unused")
    try:
        assert demo.base_url == KalshiClient.ENVIRONMENT_URLS["demo"]
        assert production.base_url == KalshiClient.ENVIRONMENT_URLS["production"]
    finally:
        await demo.close()
        await production.close()


async def test_repeated_cursor_fails_instead_of_looping_forever():
    client = client_without_credentials()
    client._make_authenticated_request.side_effect = [
        {"orders": [raw_order("one")], "cursor": "repeat"},
        {"orders": [raw_order("two")], "cursor": "repeat"},
    ]
    with pytest.raises(KalshiAPIError, match="Repeated orders cursor"):
        await client.get_all_orders(limit=1)


async def test_duplicate_pages_are_idempotent_but_conflicts_fail():
    client = client_without_credentials()
    duplicate = raw_fill("same")
    client._make_authenticated_request.side_effect = [
        {"fills": [duplicate], "cursor": "next"},
        {"fills": [duplicate], "cursor": None},
    ]
    assert [fill.fill_id for fill in await client.get_all_fills()] == ["same"]

    client._make_authenticated_request.reset_mock(side_effect=True)
    client._make_authenticated_request.side_effect = [
        {"fills": [raw_fill("same", count="1")], "cursor": "next"},
        {"fills": [raw_fill("same", count="2")], "cursor": None},
    ]
    with pytest.raises(KalshiAPIError, match="Conflicting duplicate fill"):
        await client.get_all_fills()


@pytest.mark.parametrize("field,value", [
    ("initial_count_fp", "not-a-number"),
    ("yes_price_dollars", "NaN"),
])
async def test_malformed_fixed_point_order_fields_fail_closed(field, value):
    client = client_without_credentials()
    malformed = raw_order()
    malformed[field] = value
    client._make_authenticated_request.return_value = {
        "orders": [malformed], "cursor": None,
    }
    with pytest.raises(ValueError):
        await client.get_all_orders()


async def test_missing_cursor_is_not_silently_treated_as_last_page():
    client = client_without_credentials()
    client._make_authenticated_request.return_value = {"orders": [raw_order()]}
    with pytest.raises(KalshiAPIError, match="missing its pagination cursor"):
        await client.get_all_orders()


async def test_known_demo_and_production_hosts_cannot_be_mixed(monkeypatch):
    monkeypatch.setattr(KalshiClient, "_load_private_key", lambda self: None)
    with pytest.raises(ValueError, match="Demo environment"):
        KalshiClient(
            api_key="fake", environment="demo",
            base_url=KalshiClient.ENVIRONMENT_URLS["production"],
            private_key_path="unused",
        )
    with pytest.raises(ValueError, match="Production environment"):
        KalshiClient(
            api_key="fake", environment="production",
            base_url=KalshiClient.ENVIRONMENT_URLS["demo"],
            private_key_path="unused",
        )

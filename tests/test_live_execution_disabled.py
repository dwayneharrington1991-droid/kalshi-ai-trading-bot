import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from src.clients.kalshi_client import KalshiAPIError, KalshiClient


ROOT = Path(__file__).resolve().parents[1]


def test_cli_rejects_live_order_execution_before_startup():
    result = subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "run", "--live"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "live order execution is disabled" in result.stderr
    assert "LIVE TRADING MODE ENABLED" not in result.stdout


def test_close_all_rejects_live_submission_before_account_access():
    result = subprocess.run(
        [sys.executable, str(ROOT / "cli.py"), "close-all", "--live"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "order submission is disabled" in result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_kalshi_client_rejects_non_get_requests_before_network(method):
    client = KalshiClient.__new__(KalshiClient)

    with pytest.raises(KalshiAPIError, match="only GET requests are allowed"):
        await client._make_authenticated_request(
            method, "/trade-api/v2/portfolio/events/orders"
        )


@pytest.mark.asyncio
async def test_kalshi_order_submission_and_cancellation_are_disabled():
    client = KalshiClient.__new__(KalshiClient)

    with pytest.raises(KalshiAPIError, match="Order submission is disabled"):
        await client.place_order(
            ticker="TEST-MARKET",
            client_order_id="test-order",
            side="yes",
            action="buy",
            count=1,
            yes_price=50,
        )

    with pytest.raises(KalshiAPIError, match="Order cancellation is disabled"):
        await client.cancel_order("test-order")


def test_bot_v2_kalshi_client_rejects_order_writes():
    script = dedent(
        """
        import asyncio
        from src.clients.kalshi_client import KalshiAPIError, KalshiClient

        async def verify():
            client = KalshiClient.__new__(KalshiClient)
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                try:
                    await client._make_authenticated_request(
                        method, "/trade-api/v2/portfolio/events/orders"
                    )
                except KalshiAPIError as error:
                    assert "only GET requests are allowed" in str(error)
                else:
                    raise AssertionError(f"{method} request was not rejected")
            try:
                await client.place_order(
                    ticker="TEST-MARKET",
                    client_order_id="test-order",
                    side="yes",
                    action="buy",
                    count=1,
                    yes_price=50,
                )
            except KalshiAPIError as error:
                assert "Order submission is disabled" in str(error)
            else:
                raise AssertionError("order submission was not rejected")
            try:
                await client.cancel_order("test-order")
            except KalshiAPIError as error:
                assert "Order cancellation is disabled" in str(error)
            else:
                raise AssertionError("order cancellation was not rejected")

        asyncio.run(verify())
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT / "bot_v2",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr

import pytest

from src.clients.kalshi_ws import websocket_url


def test_production_websocket_uses_dedicated_official_host():
    assert websocket_url("production") == (
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    )


def test_demo_websocket_uses_dedicated_official_host():
    assert websocket_url("demo") == (
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    )


def test_unknown_websocket_environment_fails_closed():
    with pytest.raises(ValueError, match="Unsupported Kalshi WebSocket environment"):
        websocket_url("unknown")

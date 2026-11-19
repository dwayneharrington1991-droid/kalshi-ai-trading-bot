"""Hard paper-only safeguards for the micro-scalp simulator."""
from pathlib import Path
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import httpx
import pytest

from src.micro_scalp.public_data import PublicGetClient, check_request
from src.micro_scalp.safety import LiveOrderSubmissionBlocked, PaperOrderGateway, scrub_credentials

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "micro_scalp"


def _run(code, env=None):
    import os

    full_env = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, env=full_env)


def test_guard_blocks_importing_authenticated_kalshi_client():
    r = _run(
        "from src.micro_scalp import install_guard; install_guard()\n"
        "try:\n import src.clients.kalshi_client\nexcept Exception as e: print(type(e).__name__)"
    )
    assert "LiveOrderSubmissionBlocked" in r.stdout, r.stderr


@pytest.mark.parametrize("module", ["src.jobs.execute", "src.orders.repository", "src.signals_display.account_readonly"])
def test_guard_blocks_other_order_capable_modules(module):
    r = _run(
        "from src.micro_scalp import install_guard; install_guard()\n"
        f"try:\n import {module}\nexcept Exception as e: print(type(e).__name__)"
    )
    assert "LiveOrderSubmissionBlocked" in r.stdout, r.stderr


def test_guard_refuses_to_start_if_order_module_already_loaded():
    r = _run("import src.clients.kalshi_client\nfrom src.micro_scalp import install_guard\ninstall_guard()")
    assert r.returncode != 0 and "LiveOrderSubmissionBlocked" in r.stderr


def test_guard_refuses_live_trading_environment_flag():
    r = _run("from src.micro_scalp import install_guard; install_guard()", env={"LIVE_TRADING_ENABLED": "true"})
    assert r.returncode != 0 and "refusing to start" in r.stderr


def test_guard_scrubs_credentials_from_process_environment():
    r = _run(
        "import os; from src.micro_scalp import install_guard; install_guard(); print(sorted(k for k in os.environ if 'KALSHI' in k))",
        env={"KALSHI_PRIVATE_KEY_PATH": "/x", "KALSHI_API_KEY_ID": "abc", "KALSHI_MONITOR_KEY_ID": "y"},
    )
    assert r.stdout.strip() == "[]", r.stdout + r.stderr


def test_scrub_credentials_keeps_unrelated_variables():
    env = {"KALSHI_API_KEY": "k", "KALSHI_PRIVATE_KEY_PEM": "p", "PATH": "/bin", "KALSHI_SERIES": "x"}
    removed = scrub_credentials(env)
    assert set(removed) == {"KALSHI_API_KEY", "KALSHI_PRIVATE_KEY_PEM"}
    assert env == {"PATH": "/bin", "KALSHI_SERIES": "x"}


def test_paper_gateway_never_submits_live():
    g = PaperOrderGateway()
    with pytest.raises(LiveOrderSubmissionBlocked):
        g.submit_live({"ticker": "X"})
    for name in ("place_order", "create_order", "cancel_order", "batch_create_orders", "amend_order"):
        with pytest.raises(LiveOrderSubmissionBlocked):
            getattr(g, name)

    class Real:
        real = True

    with pytest.raises(LiveOrderSubmissionBlocked):
        g.submit_paper(Real())


@pytest.mark.parametrize(
    "method,url,headers",
    [
        ("POST", "https://api.elections.kalshi.com/trade-api/v2/markets", None),
        ("DELETE", "https://api.elections.kalshi.com/trade-api/v2/markets/X", None),
        ("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders", None),
        ("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance", None),
        ("GET", "https://api.elections.kalshi.com/trade-api/v2/markets", {"KALSHI-ACCESS-KEY": "k"}),
        ("GET", "https://api.elections.kalshi.com/trade-api/v2/markets", {"Authorization": "Bearer x"}),
        ("GET", "https://evil.example.com/trade-api/v2/markets", None),
        ("GET", "http://api.elections.kalshi.com/trade-api/v2/markets", None),
        ("GET", "http://127.0.0.1:8768/api/orders", None),
    ],
)
def test_public_client_rejects_non_public_requests(method, url, headers):
    with pytest.raises(LiveOrderSubmissionBlocked):
        check_request(method, url, headers)


def test_public_client_allows_public_market_data_and_loopback_state():
    check_request("GET", "https://api.elections.kalshi.com/trade-api/v2/markets/KXBTC15M-X/orderbook")
    check_request("GET", "https://api.elections.kalshi.com/trade-api/v2/markets/trades")
    check_request("GET", "https://api.elections.kalshi.com/trade-api/v2/series/KXBTC15M/markets/X/candlesticks")
    check_request("GET", "http://127.0.0.1:8790/api/state")


def test_blocked_request_never_reaches_the_network():
    calls = []
    client = PublicGetClient(transport=httpx.MockTransport(lambda req: calls.append(req) or httpx.Response(200, json={})))
    with pytest.raises(LiveOrderSubmissionBlocked):
        client.get_json("https://api.elections.kalshi.com/trade-api/v2/portfolio/orders")
    assert calls == []
    client.kalshi("/markets", {"series_ticker": "KXBTC15M"})
    assert len(calls) == 1 and calls[0].method == "GET" and "authorization" not in calls[0].headers


def test_simulator_source_contains_no_order_submission_code():
    forbidden = ("kalshi_client", "KalshiClient", ".post(", ".put(", ".delete(", "create_order", "place_order(", "requests.")
    for path in PKG.glob("*.py"):
        text = path.read_text()
        for word in forbidden:
            if path.name == "safety.py" and word in ("kalshi_client", "create_order", "place_order("):
                continue
            assert word not in text, f"{word!r} found in {path.name}"


def test_dashboard_refuses_protected_ports():
    from src.micro_scalp.dashboard import check_port

    for port in (8765, 8767, 8768):
        with pytest.raises(SystemExit):
            check_port("127.0.0.1", port)
    check_port("127.0.0.1", 8791)


def test_dashboard_has_no_write_endpoints():
    from http.server import ThreadingHTTPServer

    from src.micro_scalp.dashboard import EngineHost, make_handler
    from src.micro_scalp.engine import Engine

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(EngineHost(Engine())))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(base + "/api/view") as r:
            assert b'"paper_trading_only": true' in r.read()
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(base + "/api/order", data=b"{}", method=method)
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req)
            assert exc.value.code == 405
    finally:
        server.shutdown()

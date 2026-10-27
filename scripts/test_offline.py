"""Run repository tests with live tests excluded and outbound network denied.

Only this test process's environment is changed. No secret values are printed,
no dotenv files are loaded, and loopback HTTP remains available for browser tests.
Run from the repository root: .venv/bin/python3 scripts/test_offline.py
"""
import os
import sys
import ipaddress

for name in list(os.environ):
    upper = name.upper()
    if any(
        part in upper
        for part in ("KALSHI", "API_KEY", "PRIVATE_KEY", "TOKEN", "PASSWORD", "SECRET")
    ):
        os.environ.pop(name, None)
os.environ.update(
    {
        "RUN_LIVE_TESTS": "0",
        "LIVE_TRADING_ENABLED": "false",
        "AUTHORITATIVE_LIVE_EXECUTION_ENABLED": "false",
        "LIVE_ORDER_SUBMISSION_KILL_SWITCH": "true",
        "MULTI_AGENT_CAN_AFFECT_TRADING": "false",
        "PYTHON_DOTENV_DISABLED": "1",
    }
)

try:
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False
except ImportError:
    pass


def loopback(host):
    if host in (None, "", "localhost", b"localhost"):
        return True
    try:
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, UnicodeError):
        return False


def offline_audit(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and not loopback(address[0]):
            raise PermissionError("Outbound networking disabled by offline test harness")
    elif event == "socket.getaddrinfo" and not loopback(args[0]):
        raise PermissionError("External DNS disabled by offline test harness")


sys.addaudithook(offline_audit)
sys.path.insert(0, os.getcwd())
import pytest

raise SystemExit(
    pytest.main(
        [
            "tests",
            "-m",
            "not live",
            "--tb=short",
            "-q",
            "--continue-on-collection-errors",
            *sys.argv[1:],
        ]
    )
)

"""Hard technical safeguards: this package can never submit a live order.

Defence in depth, each layer independently sufficient:

1. ``PAPER_TRADING_ONLY`` is a module constant with no setter, flag or env var
   that can flip it. Every simulated order passes through ``PaperOrderGateway``.
2. ``PaperOrderGateway.submit_live`` (and any attribute resembling a live order
   call) unconditionally raises ``LiveOrderSubmissionBlocked``.
3. ``install_guard`` blocks importing modules in this repository that hold an
   authenticated Kalshi client or order-execution path, and refuses to start if
   one is already imported in the process.
4. ``scrub_credentials`` removes Kalshi key material from this process's
   environment so even an accidental client construction has nothing to sign.
5. ``public_data.PublicGetClient`` only issues unauthenticated GET requests to
   an allowlist of public market-data paths (see that module).
"""
from __future__ import annotations

import importlib.abc
import os
import re
import sys

PAPER_TRADING_ONLY = True

# Modules that can authenticate to Kalshi or route an order. The simulator
# never needs them; importing them in the same process is refused.
BLOCKED_MODULE_PREFIXES = (
    "src.clients.kalshi_client",
    "src.jobs.execute",
    "src.jobs.trade",
    "src.orders",
    "src.strategies.portfolio.immediate",
    "src.strategies.unified_trading_system",
    "src.signals_display.account_readonly",
    "kalshi_python",
    "kalshi",
)

CREDENTIAL_ENV_PATTERN = re.compile(r"KALSHI.*(KEY|SECRET|PASSWORD|TOKEN|PEM|EMAIL|CREDENTIAL)", re.IGNORECASE)
LIVE_ENABLE_ENV = ("LIVE_TRADING_ENABLED", "MICRO_SCALP_LIVE", "ENABLE_LIVE_TRADING")


class LiveOrderSubmissionBlocked(RuntimeError):
    """Raised on any attempt to reach a real order path."""


class _ImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _is_blocked(fullname):
            raise LiveOrderSubmissionBlocked(
                f"PAPER-ONLY: importing order-capable module '{fullname}' is blocked in the micro-scalp simulator"
            )
        return None


def _is_blocked(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in BLOCKED_MODULE_PREFIXES)


_BLOCKER = _ImportBlocker()


def install_guard() -> None:
    """Idempotently activate the guard for this process."""
    for flag in LIVE_ENABLE_ENV:
        if os.environ.get(flag, "").strip().lower() in {"1", "true", "yes", "on"}:
            raise LiveOrderSubmissionBlocked(
                f"PAPER-ONLY: environment variable {flag} requests live trading; refusing to start"
            )
    loaded = sorted(name for name in list(sys.modules) if _is_blocked(name))
    if loaded:
        raise LiveOrderSubmissionBlocked(
            "PAPER-ONLY: order-capable modules already loaded in this process: " + ", ".join(loaded)
        )
    if _BLOCKER not in sys.meta_path:
        sys.meta_path.insert(0, _BLOCKER)
    scrub_credentials()


def scrub_credentials(environ=None) -> list[str]:
    """Remove Kalshi key material from the (process) environment."""
    env = os.environ if environ is None else environ
    removed = [k for k in list(env) if CREDENTIAL_ENV_PATTERN.search(k)]
    for key in removed:
        env.pop(key, None)
    return removed


class PaperOrderGateway:
    """The ONLY order interface in the simulator. It has no network access."""

    mode = "PAPER"

    def __init__(self):
        if not PAPER_TRADING_ONLY:  # pragma: no cover - constant cannot change
            raise LiveOrderSubmissionBlocked("PAPER_TRADING_ONLY must be True")
        self.paper_orders = []

    def submit_paper(self, order) -> None:
        if getattr(order, "real", False):
            raise LiveOrderSubmissionBlocked("PAPER-ONLY: order marked real")
        self.paper_orders.append(order)

    def submit_live(self, *_args, **_kwargs):
        raise LiveOrderSubmissionBlocked("PAPER-ONLY: live order submission is permanently disabled")

    # Any name that resembles a real order call is refused, too.
    def __getattr__(self, name):
        if re.search(r"(place|create|submit|amend|cancel|modify|batch).*order|order.*(place|create|submit)", name, re.I):
            raise LiveOrderSubmissionBlocked(f"PAPER-ONLY: '{name}' is not available")
        raise AttributeError(name)

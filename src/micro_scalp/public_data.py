"""Unauthenticated, GET-only access to public market data.

The client refuses (before any network IO):
- any HTTP method other than GET;
- any host other than the public Kalshi API or loopback (the read-only signals
  dashboard of a *separate* process, used only for its existing AI state);
- any Kalshi path outside the public market-data allowlist (no /portfolio,
  /orders, /exchange-auth, ...);
- any request carrying authentication headers.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .safety import LiveOrderSubmissionBlocked

KALSHI_HOST = "api.elections.kalshi.com"
KALSHI_BASE = f"https://{KALSHI_HOST}/trade-api/v2"
ALLOWED_KALSHI_PREFIXES = ("/trade-api/v2/markets", "/trade-api/v2/series/", "/trade-api/v2/events")
FORBIDDEN_FRAGMENTS = ("portfolio", "order", "login", "logout", "api_keys", "transfer", "withdraw", "deposit")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Ports owned by the user's existing deployments. The simulator never serves on
# them; it may only *read* from a loopback dashboard the operator points it to.
PROTECTED_PORTS = {8765, 8767, 8768}
AUTH_HEADERS = ("authorization", "kalshi-access-key", "kalshi-access-signature", "kalshi-access-timestamp", "cookie")


def check_request(method: str, url: str, headers: dict | None = None) -> None:
    if method.upper() != "GET":
        raise LiveOrderSubmissionBlocked(f"PAPER-ONLY: HTTP {method} is blocked; only GET is allowed")
    for name in headers or {}:
        if name.lower() in AUTH_HEADERS:
            raise LiveOrderSubmissionBlocked("PAPER-ONLY: authenticated requests are blocked")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()
    if any(fragment in path for fragment in FORBIDDEN_FRAGMENTS) and not path.endswith("/orderbook"):
        raise LiveOrderSubmissionBlocked(f"PAPER-ONLY: path '{parts.path}' is not public market data")
    if host == KALSHI_HOST:
        if parts.scheme != "https" or not path.startswith(ALLOWED_KALSHI_PREFIXES):
            raise LiveOrderSubmissionBlocked(f"PAPER-ONLY: Kalshi path '{parts.path}' is not allowlisted")
        return
    if host in LOOPBACK_HOSTS and parts.scheme == "http" and path.startswith("/api/state"):
        return
    raise LiveOrderSubmissionBlocked(f"PAPER-ONLY: host '{host}' / path '{parts.path}' is not allowlisted")


class PublicGetClient:
    def __init__(self, timeout: float = 10.0, transport=None):
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)

    def get_json(self, url: str, params: dict | None = None):
        check_request("GET", url)
        response = self._http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def kalshi(self, path: str, params: dict | None = None):
        return self.get_json(KALSHI_BASE + path, params)

    def close(self):
        self._http.close()

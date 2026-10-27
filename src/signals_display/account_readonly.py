"""Isolated Kalshi authenticated monitoring. Strict GET-only endpoint allowlist.

No execution client import, generic browser proxy, order mutation, or transfers.
Credentials are loaded only on explicit server-side account-mode startup.
"""
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3
import time
import hashlib

import httpx

ORIGIN = "https://api.elections.kalshi.com"
PATHS = frozenset(
    "/trade-api/v2/" + p
    for p in (
        "portfolio/balance",
        "portfolio/fills",
        "portfolio/orders",
        "portfolio/positions",
        "api_keys",
    )
)


class AccountReadError(Exception):
    pass


class ReadOnlyKalshi:
    def __init__(self, key_id, signer, get=None):
        self._key_id = key_id
        self._signer = signer
        self._get = get or httpx.get
        self.credential_binding = hashlib.sha256(key_id.encode()).hexdigest()
        self._permissions_cache = None
        self._permissions_time = 0

    @classmethod
    def from_environment(cls):
        import os

        key_id = os.environ.get("KALSHI_MONITOR_KEY_ID", "")
        pem = os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PEM", "")
        path = os.environ.get("KALSHI_MONITOR_PRIVATE_KEY_PATH", "")
        if not key_id or (not pem and not path):
            raise AccountReadError(
                "Read-only account mode needs server-side monitoring key settings; never enter credentials in the browser or chat"
            )
        try:
            if not pem:
                source = Path(path)
                if source.stat().st_mode & 0o077:
                    raise AccountReadError(
                        "Monitoring private-key file must have owner-only permissions (chmod 600)"
                    )
                pem = source.read_text()
            from cryptography.hazmat.primitives import serialization, hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            key = serialization.load_pem_private_key(pem.encode(), password=None)
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

            if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
                raise AccountReadError("Monitoring key must be RSA with at least 2048 bits")

            def sign(message):
                return base64.b64encode(
                    key.sign(
                        message.encode(),
                        padding.PSS(
                            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
                        ),
                        hashes.SHA256(),
                    )
                ).decode()

            return cls(key_id, sign)
        except AccountReadError:
            raise
        except Exception:
            raise AccountReadError(
                "Monitoring key could not be loaded securely; check server-side key format and dependency"
            ) from None

    def request(self, method, path, params=None):
        if method != "GET" or path not in PATHS:
            raise AccountReadError("Endpoint/method blocked by read-only monitoring allowlist")
        params = params or {}
        if set(params) - {"cursor", "limit", "min_ts"}:
            raise AccountReadError("Unsupported monitoring query parameter")
        stamp = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": self._signer(stamp + "GET" + path),
            "Accept": "application/json",
        }
        try:
            response = self._get(
                ORIGIN + path, params=params, headers=headers, timeout=10, follow_redirects=False
            )
            if response.status_code != 200:
                raise AccountReadError(
                    f"Account read failed (HTTP {response.status_code}); no mutation attempted"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise AccountReadError("Unsupported account response")
            return data
        except AccountReadError:
            raise
        except Exception:
            raise AccountReadError(
                "Account read unavailable; credentials and response bodies are redacted"
            ) from None

    def pages(self, path, key, params=None):
        items = []
        cursor = None
        seen = set()
        for _ in range(100):
            query = {**(params or {}), "limit": 200}
            if cursor:
                query["cursor"] = cursor
            page = self.request("GET", path, query)
            if not isinstance(page.get(key), list):
                raise AccountReadError("Incomplete account schema; reconciliation withheld")
            items.extend(page[key])
            cursor = page.get("cursor")
            if not cursor:
                return items
            if cursor in seen:
                raise AccountReadError(
                    "Repeated account pagination cursor; reconciliation withheld"
                )
            seen.add(cursor)
        raise AccountReadError(
            "Account pagination limit reached; no incomplete position reconciliation"
        )

    def permissions(self):
        if self._permissions_cache is not None and time.monotonic() - self._permissions_time < 60:
            return self._permissions_cache
        result = self._verify_permissions()
        self._permissions_cache = result
        self._permissions_time = time.monotonic()
        return result

    def _verify_permissions(self):
        try:
            keys = self.request("GET", "/trade-api/v2/api_keys").get("api_keys", [])
            current = next(
                (k for k in keys if k.get("api_key_id", k.get("id")) == self._key_id), None
            )
            scopes = current.get("scopes") if current else None
            if isinstance(scopes, list) and set(scopes) == {"read"}:
                return {
                    "status": "READ_ONLY_SCOPE_REPORTED",
                    "read_only_verified": True,
                    "warning": "API metadata reports only read scope; GET allowlist is still enforced",
                }
            if isinstance(scopes, list) and "write" in scopes:
                return {
                    "status": "TRADING_CAPABLE_KEY",
                    "read_only_verified": False,
                    "warning": "This key permits trading outside this application. This separate client blocks all writes and transfers.",
                }
        except AccountReadError:
            pass
        return {
            "status": "PERMISSIONS_UNVERIFIED",
            "read_only_verified": False,
            "warning": "Broker read-only permissions could not be verified. Treat the key as possibly trading-capable; this client enforces the GET-only allowlist.",
        }

    def snapshot(self, min_ts=None):
        if getattr(self, "require_readonly", False) and not self.permissions().get(
            "read_only_verified"
        ):
            raise AccountReadError(
                "Read-only key permissions no longer verified; account sync withheld"
            )
        # Never infer holdings/fills from order status. All pages must complete.
        positions = self.pages("/trade-api/v2/portfolio/positions", "market_positions")
        fills = self.pages(
            "/trade-api/v2/portfolio/fills", "fills", {"min_ts": min_ts} if min_ts else None
        )
        orders = self.pages("/trade-api/v2/portfolio/orders", "orders")
        balance = self.request("GET", "/trade-api/v2/portfolio/balance")
        return {
            "positions": positions,
            "fills": fills,
            "orders": orders,
            "balance": balance,
            "permissions": self.permissions(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "complete": True,
        }


def decimal(value):
    try:
        d = Decimal(str(value))
        if not d.is_finite():
            raise ValueError("nonfinite")
        return d
    except (InvalidOperation, ValueError):
        raise AccountReadError("Invalid numeric account field") from None


def normalized_fill(raw):
    identifier = raw.get("fill_id") or raw.get("trade_id")
    side = raw.get("side") or raw.get("outcome_side") or raw.get("purchased_side")
    action = raw.get("action")
    if not identifier or side not in ("yes", "no") or action not in ("buy", "sell"):
        raise AccountReadError("Fill schema/side/action unverified; cost basis withheld")
    quantity = decimal(raw.get("count_fp", raw.get("count")))
    price = (
        decimal(raw[side + "_price_dollars"])
        if raw.get(side + "_price_dollars") is not None
        else decimal(raw[side + "_price"]) / 100
    )
    fees = (
        decimal(raw["fee_cost_dollars"])
        if raw.get("fee_cost_dollars") is not None
        else decimal(raw["fee_cost"]) / 100
        if raw.get("fee_cost") is not None
        else None
    )
    if quantity <= 0 or not 0 <= price <= 1 or (fees is not None and fees < 0):
        raise AccountReadError("Invalid confirmed fill values")
    at = raw.get("created_time")
    if not at:
        ts = raw.get("ts_ms", raw.get("ts"))
        at = (
            datetime.fromtimestamp(
                float(ts) / (1000 if raw.get("ts_ms") is not None else 1), timezone.utc
            ).isoformat()
            if ts is not None
            else None
        )
    from .model import timestamp

    if not at:
        raise AccountReadError("Confirmed fill timestamp missing")
    at = timestamp(at).astimezone(timezone.utc).isoformat()
    return {
        "id": str(identifier),
        "ticker": str(raw.get("ticker", raw.get("market_ticker", ""))),
        "side": side,
        "action": action,
        "quantity": float(quantity),
        "price": float(price),
        "fees": float(fees) if fees is not None else None,
        "at": at,
        "order_id": str(raw.get("order_id", "")),
    }


def fifo_basis(fills, ticker, signed_position):
    lots = []
    unknown = False
    for f in sorted((f for f in fills if f["ticker"] == ticker), key=lambda f: (f["at"], f["id"])):
        sign = (1 if f["side"] == "yes" else -1) * (1 if f["action"] == "buy" else -1)
        q = f["quantity"]
        cost = f["price"] if f["action"] == "buy" else 1 - f["price"]
        rate = f["fees"] / q if f["fees"] is not None else None
        while q > 1e-9 and lots and lots[0]["sign"] != sign:
            used = min(q, lots[0]["quantity"])
            q -= used
            lots[0]["quantity"] -= used
            if lots[0]["quantity"] <= 1e-9:
                lots.pop(0)
        if q > 1e-9:
            lots.append(
                {"sign": sign, "quantity": q, "cost": cost, "fee_per_unit": rate, "at": f["at"]}
            )
    net = sum(l["sign"] * l["quantity"] for l in lots)
    qty = abs(signed_position)
    if abs(net - signed_position) > 1e-6 or qty == 0:
        return {
            "verified": False,
            "reason": "Confirmed fill history/fees do not fully reconcile with the authoritative position; no assumed entry basis",
        }
    return {
        "verified": all(l["fee_per_unit"] is not None for l in lots),
        "quantity_reconciled": True,
        "reason": "Entry fees unavailable"
        if any(l["fee_per_unit"] is None for l in lots)
        else None,
        "entry_price": sum(l["cost"] * l["quantity"] for l in lots) / qty,
        "entry_fees": sum(l["fee_per_unit"] * l["quantity"] for l in lots)
        if all(l["fee_per_unit"] is not None for l in lots)
        else None,
        "entered_at": min((l["at"] for l in lots), default=None),
        "method": "FIFO net exposure from confirmed fills; opposite-side netting uses complementary payout prices, not an invented fill",
    }


def confirmed_cycles(fills):
    """FIFO realized cycles from observed confirmed fills; never order-status fills."""
    cycles = []
    for ticker in sorted({f["ticker"] for f in fills}):
        lots = []
        cycle = None
        for f in sorted(
            (f for f in fills if f["ticker"] == ticker), key=lambda f: (f["at"], f["id"])
        ):
            sign = (1 if f["side"] == "yes" else -1) * (1 if f["action"] == "buy" else -1)
            q = f["quantity"]
            cost = f["price"] if f["action"] == "buy" else 1 - f["price"]
            rate = f["fees"] / q if f["fees"] is not None else None
            while q > 1e-9 and lots and lots[0]["sign"] != sign:
                lot = lots[0]
                used = min(q, lot["quantity"])
                q -= used
                lot["quantity"] -= used
                if rate is None or lot["fee"] is None:
                    cycle["fees_known"] = False
                else:
                    cycle["realized_pnl"] += used * ((1 - cost) - lot["cost"] - rate - lot["fee"])
                cycle["closed_quantity"] += used
                if lot["quantity"] <= 1e-9:
                    lots.pop(0)
                if not lots:
                    cycle["closed_at"] = f["at"]
                    cycle["status"] = "CLOSED"
                    cycle = None
            if q > 1e-9:
                if cycle is None:
                    cycle = {
                        "cycle_id": ticker + ":" + f["id"] + ":" + str(sign),
                        "ticker": ticker,
                        "side": "up" if sign > 0 else "down",
                        "opened_at": f["at"],
                        "closed_at": None,
                        "status": "OPEN",
                        "entry_quantity_total": 0.0,
                        "entry_principal_total": 0.0,
                        "entry_fees_total": 0.0,
                        "closed_quantity": 0.0,
                        "realized_pnl": 0.0,
                        "fees_known": True,
                    }
                    cycles.append(cycle)
                cycle["entry_quantity_total"] += q
                cycle["entry_principal_total"] += q * cost
                if rate is None:
                    cycle["fees_known"] = False
                else:
                    cycle["entry_fees_total"] += q * rate
                lots.append({"sign": sign, "quantity": q, "cost": cost, "fee": rate})
        for cycle in [c for c in cycles if c["ticker"] == ticker]:
            cycle["average_entry_price"] = (
                cycle["entry_principal_total"] / cycle["entry_quantity_total"]
            )
            cycle["total_entry_cost"] = (
                cycle["entry_principal_total"] + cycle["entry_fees_total"]
                if cycle["fees_known"]
                else None
            )
            if not cycle["fees_known"]:
                cycle["realized_pnl"] = None
            cycle["source"] = "confirmed-fill FIFO observed history, not brokerage tax accounting"
    return cycles

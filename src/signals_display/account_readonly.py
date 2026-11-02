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


def credential_status():
    import os

    return {
        name: bool(os.environ.get(name))
        for name in (
            "KALSHI_MONITOR_KEY_ID",
            "KALSHI_MONITOR_PRIVATE_KEY_PEM",
            "KALSHI_MONITOR_PRIVATE_KEY_PATH",
        )
    }


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
        self._subaccount = 0

    @classmethod
    def from_environment(cls):
        import os

        key_id = os.environ.get("KALSHI_MONITOR_KEY_ID", "").strip()
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
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)) or (
                isinstance(key, RSAPrivateKey) and key.key_size < 2048
            ):
                raise AccountReadError(
                    "Unsupported monitoring key: require RSA >=2048 bits or Ed25519"
                )

            def sign(message):
                if isinstance(key, Ed25519PrivateKey):
                    return base64.b64encode(key.sign(message.encode())).decode()
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
        if set(params) - {"cursor", "limit", "min_ts", "subaccount", "status"}:
            raise AccountReadError("Unsupported monitoring query parameter")
        if "status" in params and (
            path != "/trade-api/v2/portfolio/orders" or params["status"] != "resting"
        ):
            raise AccountReadError("Unsupported pending-order monitoring filter")
        if "subaccount" in params and (
            not path.startswith("/trade-api/v2/portfolio/")
            or type(params["subaccount"]) is not int
            or not 0 <= params["subaccount"] <= 63
        ):
            raise AccountReadError("Unsupported read-only account scope")
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
                    f"Authenticated GET {path} failed (HTTP {response.status_code}); no mutation attempted"
                )
            data = response.json()
            if not isinstance(data, dict):
                raise AccountReadError("Unsupported account response")
            return data
        except AccountReadError:
            raise
        except Exception as exc:
            raise AccountReadError(
                "Authenticated GET "
                + path
                + " unavailable ("
                + type(exc).__name__
                + "); credentials and response bodies are redacted"
            ) from None

    def websocket_headers(self):
        """Sign only the fixed market-data WebSocket GET handshake.

        This does not expose a generic signer or permit REST mutations. The
        separate consumer accepts only KXBTC15M order-book subscriptions.
        """
        permissions = self.permissions()
        if not permissions.get("read_only_verified") or permissions.get("account_access_blocker"):
            raise AccountReadError("Market stream disabled: read-only permissions are not verified")
        stamp = str(int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": self._signer(stamp + "GET/trade-api/ws/v2"),
        }

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
        if self._permissions_cache is not None and time.monotonic() - self._permissions_time < (
            60 if self._permissions_cache.get("read_only_verified") else 10
        ):
            return self._permissions_cache
        result = self._verify_permissions()
        self._permissions_cache = result
        self._permissions_time = time.monotonic()
        return result

    def _verify_permissions(self):
        data = {}
        result = {
            "status": "PERMISSIONS_UNVERIFIED",
            "read_only_verified": False,
            "authentication": "UNVERIFIED",
            "endpoint": "/trade-api/v2/api_keys",
            "origin": ORIGIN,
        }
        try:
            data = self.request("GET", "/trade-api/v2/api_keys")
            result["authentication"] = "AUTHENTICATED_GET_SUCCEEDED"
            if not isinstance(data.get("api_keys"), list) or any(
                not isinstance(k, dict) for k in data.get("api_keys", [])
            ):
                result["blocker"] = "KEY_METADATA_SCHEMA_UNSUPPORTED: response has no api_keys list"
            else:
                current = next(
                    (
                        k
                        for k in data["api_keys"]
                        if k.get("api_key_id", k.get("id")) == self._key_id
                    ),
                    None,
                )
                if current is None:
                    result[
                        "blocker"
                    ] = "CURRENT_KEY_NOT_IN_METADATA: authenticated response did not identify this key"
                else:
                    scopes = current.get("scopes")
                    valid_scopes = isinstance(scopes, list) and all(
                        isinstance(scope, str) for scope in scopes
                    )
                    read_scopes = {"read", "read::portfolio_balance", "read::block_trade_accept"}
                    if valid_scopes and "read" in scopes and set(scopes) <= read_scopes:
                        result.update(
                            status="READ_ONLY_SCOPE_REPORTED", read_only_verified=True, blocker=None
                        )
                    elif valid_scopes and any(
                        scope == "write"
                        or scope.startswith("write::")
                        or scope
                        in (
                            "trade",
                            "trading",
                            "transfer",
                            "transfers",
                            "orders:write",
                            "transfers:write",
                        )
                        for scope in scopes
                    ):
                        result.update(
                            status="TRADING_CAPABLE_KEY",
                            blocker="MUTATION_SCOPE_PRESENT: trading/transfer permissions are not read-only",
                        )
                    else:
                        result[
                            "blocker"
                        ] = "READ_ONLY_SCOPE_NOT_PROVEN: broad read scope required for positions/orders/fills, with no unknown or mutation scopes; account reads withheld"
                    bound = current.get("subaccount")
                    if bound is not None:
                        if type(bound) is not int or not 0 <= bound <= 63:
                            result.update(
                                status="PERMISSIONS_UNVERIFIED",
                                read_only_verified=False,
                                blocker="INVALID_SUBACCOUNT_BINDING: account scope cannot be verified",
                            )
                        else:
                            self._subaccount = bound
                            result["account_scope"] = "key-restricted subaccount"
                    else:
                        self._subaccount = 0
                        result["account_scope"] = "primary account"
                    if current.get("fcm_subtrader_id") is not None:
                        result[
                            "account_access_blocker"
                        ] = "FCM_BOUND_KEY: official API documents this key binding as denied on every REST endpoint"
        except AccountReadError as exc:
            result["blocker"] = str(exc)  # request() emits only redacted, static diagnostics.
        expiry = data.get("api_key_region_expiration_ts")
        result["location_attestation"] = (
            ("EXPIRED" if expiry < time.time() else "CURRENT")
            if type(expiry) is int
            else "NOT_REPORTED"
        )
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        result["warning"] = (
            "API metadata reports exclusively read scope; GET allowlist still enforced"
            if result["read_only_verified"]
            else result["blocker"]
            + ". Cannot verify absence of trading/transfer permission; account monitoring withheld."
        )
        return result

    def snapshot(self, min_ts=None):
        if getattr(self, "require_readonly", False):
            permissions = self.permissions()
            if not permissions.get("read_only_verified"):
                raise AccountReadError(
                    "Read-only key permissions no longer verified; account sync withheld: "
                    + str(permissions.get("blocker"))
                )
            if permissions.get("account_access_blocker"):
                raise AccountReadError(permissions["account_access_blocker"])
        # Never infer holdings/fills from order status. All pages must complete.
        scoped = {"subaccount": self._subaccount}
        positions = self.pages("/trade-api/v2/portfolio/positions", "market_positions", scoped)
        fills = self.pages(
            "/trade-api/v2/portfolio/fills",
            "fills",
            {**scoped, **({"min_ts": min_ts} if min_ts else {})},
        )
        orders = self.pages(
            "/trade-api/v2/portfolio/orders", "orders", {**scoped, "status": "resting"}
        )
        balance = self.request("GET", "/trade-api/v2/portfolio/balance", scoped)
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
    legacy_side = raw.get("side")
    legacy_action = raw.get("action")
    outcome = raw.get("outcome_side")
    book_side = raw.get("book_side")
    direction_basis = "legacy side/action"
    legacy_warning = None
    if outcome is not None or book_side is not None:
        direction = outcome if outcome is not None else {"bid": "yes", "ask": "no"}.get(book_side)
        if direction not in ("yes", "no") or (
            book_side is not None and book_side != ("bid" if direction == "yes" else "ask")
        ):
            raise AccountReadError("Conflicting/unsupported canonical fill direction")
        # Canonical fields are authoritative per the official migration guide.
        # Deprecated disagreements are retained and audited, never used to flip direction.
        legacy_warning = None
        if legacy_side is not None or legacy_action is not None:
            if legacy_side not in ("yes", "no") or legacy_action not in ("buy", "sell"):
                legacy_warning = "DEPRECATED_DIRECTION_INCOMPLETE_OR_UNSUPPORTED"
            else:
                expected = (
                    legacy_side
                    if legacy_action == "buy"
                    else ("no" if legacy_side == "yes" else "yes")
                )
                if expected != direction:
                    legacy_warning = "CANONICAL_LEGACY_DIRECTION_DISAGREEMENT"
        side, action = direction, "buy"
        direction_basis = "canonical outcome exposure; buy is an economic normalization, not an inferred submitted order"
    else:
        side, action = legacy_side, legacy_action
    if not identifier or side not in ("yes", "no") or action not in ("buy", "sell"):
        raise AccountReadError("Fill schema/side/action unverified; cost basis withheld")
    quantity = decimal(raw.get("count_fp", raw.get("count")))
    price = (
        decimal(raw[side + "_price_dollars"])
        if raw.get(side + "_price_dollars") is not None
        else decimal(raw[side + "_price"]) / 100
    )
    if raw.get("yes_price_dollars") is not None and raw.get("no_price_dollars") is not None:
        if abs(decimal(raw["yes_price_dollars"]) + decimal(raw["no_price_dollars"]) - 1) > Decimal(
            "0.000001"
        ):
            raise AccountReadError("Incompatible YES/NO fill prices")
    if raw.get("fee_cost_dollars") is not None:
        fees = decimal(raw["fee_cost_dollars"])
    elif isinstance(raw.get("fee_cost"), str):
        fees = decimal(raw["fee_cost"])  # Current FixedPointDollars schema.
    elif type(raw.get("fee_cost")) is int and outcome is None and book_side is None:
        fees = decimal(raw["fee_cost"]) / 100  # Legacy integer-cent payload.
    elif raw.get("fee_cost") is None:
        fees = None
    else:
        raise AccountReadError("Ambiguous fill fee units; cost basis withheld")
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
        "direction_basis": direction_basis,
        "direction_warning": legacy_warning,
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

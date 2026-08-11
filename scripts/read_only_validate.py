#!/usr/bin/env python3
"""Run one authenticated GET-only Kalshi account validation cycle."""

import argparse
import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENVIRONMENT_URLS = {
    "demo": "https://external-api.demo.kalshi.co",
    "production": "https://external-api.kalshi.com",
}
INGESTION_GET_ENDPOINTS = {"/trade-api/v2/events"}
NAMED_READ_METHODS = {
    "get_balance", "get_positions", "get_orders", "get_order", "get_fills",
    "get_all_orders", "get_all_fills", "get_markets", "get_market",
    "get_events", "get_event", "get_milestones",
    "get_orderbook",
    "_make_authenticated_request",
}


class ReadOnlyValidationError(RuntimeError):
    """Secret-safe validation or authenticated-read failure."""


def _copy_external_position_context(source_path: str, validation_path: str) -> int:
    """Copy only audited manual-position facts into the disposable validator DB."""
    source = Path(source_path).resolve()
    if not source.is_file():
        raise ReadOnlyValidationError("persistent reconciliation ledger is unavailable")
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        ) as source_db:
            rows = source_db.execute("""
                SELECT market_id, side, quantity, source, exchange_order_id, exchange_fill_id,
                       first_observed_at, last_verified_at, status, metadata_json
                FROM external_account_positions WHERE status = 'active'
            """).fetchall()
        with contextlib.closing(sqlite3.connect(validation_path)) as validation_db:
            validation_db.executemany("""
                INSERT INTO external_account_positions
                (market_id, side, quantity, source, exchange_order_id, exchange_fill_id,
                 first_observed_at, last_verified_at, status, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            validation_db.commit()
        return len(rows)
    except sqlite3.Error:
        raise ReadOnlyValidationError("persistent external-position context is unavailable") from None


class ReadOnlyAccountClient:
    """Capability-limited facade exposing named reads and no write operations."""

    __slots__ = ("__client", "environment", "base_url")

    def __init__(self, client: Any, environment: str):
        self.__client = client
        self.environment = environment
        self.base_url = ENVIRONMENT_URLS[environment]

    def __getattribute__(self, name: str):
        if name == "_ReadOnlyAccountClient__client":
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    async def __read(self, method: str, *args, **kwargs):
        if method not in NAMED_READ_METHODS:
            raise ReadOnlyValidationError("operation is not an approved account read")
        try:
            client = object.__getattribute__(self, "_ReadOnlyAccountClient__client")
            # The underlying client may log raw API/authentication exception
            # details. Discard its direct output and surface only fixed errors.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return await getattr(client, method)(*args, **kwargs)
        except Exception:
            raise ReadOnlyValidationError("authenticated account read failed") from None

    async def _make_authenticated_request(self, method: str, endpoint: str, **kwargs):
        if method.upper() != "GET":
            raise ReadOnlyValidationError("read-only validation permits GET requests only")
        if endpoint not in INGESTION_GET_ENDPOINTS:
            raise ReadOnlyValidationError("GET endpoint is not approved for read-only validation")
        return await self.__read("_make_authenticated_request", method, endpoint, **kwargs)

    async def get_balance(self, *args, **kwargs):
        return await self.__read("get_balance", *args, **kwargs)

    async def get_positions(self, *args, **kwargs):
        return await self.__read("get_positions", *args, **kwargs)

    async def get_orders(self, *args, **kwargs):
        return await self.__read("get_orders", *args, **kwargs)

    async def get_order(self, *args, **kwargs):
        return await self.__read("get_order", *args, **kwargs)

    async def get_fills(self, *args, **kwargs):
        return await self.__read("get_fills", *args, **kwargs)

    async def get_all_orders(self, *args, **kwargs):
        return await self.__read("get_all_orders", *args, **kwargs)

    async def get_all_fills(self, *args, **kwargs):
        return await self.__read("get_all_fills", *args, **kwargs)

    async def get_markets(self, *args, **kwargs):
        return await self.__read("get_markets", *args, **kwargs)

    async def get_market(self, *args, **kwargs):
        return await self.__read("get_market", *args, **kwargs)

    async def get_events(self, *args, **kwargs):
        return await self.__read("get_events", *args, **kwargs)

    async def get_event(self, *args, **kwargs):
        return await self.__read("get_event", *args, **kwargs)

    async def get_milestones(self, *args, **kwargs):
        return await self.__read("get_milestones", *args, **kwargs)

    async def get_orderbook(self, *args, **kwargs):
        return await self.__read("get_orderbook", *args, **kwargs)


def _is_set(value: Optional[str]) -> bool:
    return bool(value and value.strip())


def credential_status(environment: Mapping[str, str]) -> dict:
    key_path = environment.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
    return {
        "KALSHI_API_KEY": _is_set(environment.get("KALSHI_API_KEY")),
        "KALSHI_PRIVATE_KEY_FILE": Path(key_path).is_file(),
    }


def validate_read_only_safety(environment: Mapping[str, str]) -> str:
    selected = environment.get("KALSHI_ENVIRONMENT", "").strip().lower()
    if selected not in ENVIRONMENT_URLS:
        raise ReadOnlyValidationError("KALSHI_ENVIRONMENT must be demo or production")
    if environment.get("AUTHORITATIVE_LIVE_EXECUTION_ENABLED", "").lower() != "false":
        raise ReadOnlyValidationError("authoritative execution must remain disabled")
    if environment.get("LIVE_ORDER_SUBMISSION_KILL_SWITCH", "true").lower() != "true":
        raise ReadOnlyValidationError("live order submission kill switch must remain enabled")
    if environment.get("RECONCILIATION_SHADOW_MODE", "").lower() != "true":
        raise ReadOnlyValidationError("reconciliation shadow mode must be explicitly enabled")
    if selected == "production" and (
        environment.get("READ_ONLY_ACCOUNT_VALIDATION", "false").lower() != "true"
    ):
        raise ReadOnlyValidationError(
            "production requires READ_ONLY_ACCOUNT_VALIDATION=true"
        )
    if not all(credential_status(environment).values()):
        raise ReadOnlyValidationError("account credentials are incomplete")
    return selected


def _position_count(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    positions = response.get(
        "market_positions", response.get("positions", response.get("event_positions", []))
    )
    return len(positions) if isinstance(positions, list) else 0


async def _close_client(client: Any) -> None:
    """Close without allowing raw client exception output to reach the console."""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            await client.close()
    except Exception:
        raise ReadOnlyValidationError("account client cleanup failed") from None


async def run_read_only_validation(
    *,
    environment: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[..., Any]] = None,
    ingestion_runner: Optional[Callable[..., Any]] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Execute exactly one bounded account-read and shadow-reconciliation cycle."""
    environment = environment or os.environ
    selected = validate_read_only_safety(environment)

    from src.clients.kalshi_client import KalshiClient
    from src.jobs.ingest import run_ingestion
    from src.orders.readiness import ReadinessContext, SubmissionReadinessEvaluator
    from src.orders.reconciler import OrderReconciler
    from src.orders.repository import OrderRepository
    from src.utils.database import DatabaseManager

    if client_factory:
        client = client_factory(environment=selected)
    else:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            client = KalshiClient(environment=selected)
    expected_url = ENVIRONMENT_URLS[selected]
    if (
        getattr(client, "environment", None) != selected
        or getattr(client, "base_url", None) != expected_url
    ):
        await _close_client(client)
        raise ReadOnlyValidationError("Kalshi client environment or host mismatch")

    temporary_path = None
    try:
        if db_path is None:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix="kalshi-read-only-", suffix=".db"
            )
            os.close(descriptor)
            db_path = temporary_path

        read_client = ReadOnlyAccountClient(client, selected)
        manager = DatabaseManager(db_path)
        repository = OrderRepository(db_path)
        ingestion_runner = ingestion_runner or run_ingestion
        await manager.initialize()
        if temporary_path and environment.get("DB_PATH"):
            _copy_external_position_context(environment["DB_PATH"], db_path)

        # Explicit account snapshot. Reconciliation intentionally performs its
        # own bounded authoritative reads afterward.
        balance_response = await read_client.get_balance()
        positions_response = await read_client.get_positions()
        remote_orders = await read_client.get_all_orders(limit=1000)
        remote_fills = await read_client.get_all_fills(limit=1000)

        ingestion_result = await ingestion_runner(
            manager, asyncio.Queue(), kalshi_client=read_client
        )
        scan_stats = ingestion_result if isinstance(ingestion_result, dict) else {}
        reconciler = OrderReconciler(
            repository, read_client, shadow_mode=True, paper_mode=False,
            project_positions=False,
        )
        reconciliation = await reconciler.reconcile(trigger="read_only_validation", full=True)
        total_markets = await manager.get_market_count()
        eligible_markets = len(await manager.get_eligible_markets(
            volume_min=0, max_days_to_expiry=3
        ))
        readiness = await SubmissionReadinessEvaluator(repository).evaluate(
            ReadinessContext(
                live_mode=True,
                authoritative_execution_enabled=False,
                reconciliation_enabled=True,
                reconciliation_shadow_mode=True,
                kill_switch=True,
                configured_environment=selected,
                client_environment=selected,
                production_acknowledgement="",
                reconciliation_max_age_seconds=30,
                total_markets=total_markets,
                eligible_markets=eligible_markets,
            ),
            scope="cycle",
        )
        health = await repository.get_reconciliation_health(30)
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            alert_row = await (await db.execute(
                "SELECT COUNT(*) FROM reconciliation_alerts WHERE resolved_at IS NULL"
            )).fetchone()
            alert_count = int(alert_row[0])
        balance = balance_response.get("balance") if isinstance(balance_response, dict) else None
        if not isinstance(balance, (int, float)) or isinstance(balance, bool):
            balance = None
        return {
            "environment": selected,
            "endpoint": expected_url,
            "balance": balance,
            "position_count": _position_count(positions_response),
            "order_count": len(remote_orders),
            "fill_count": len(remote_fills),
            "market_count": total_markets,
            "eligible_market_count": eligible_markets,
            "scan_stats": scan_stats,
            "reconciliation": reconciliation,
            "health": health,
            "alert_count": alert_count,
            "readiness": readiness,
        }
    finally:
        try:
            await _close_client(client)
        finally:
            if temporary_path:
                removed = False
                for _ in range(20):
                    try:
                        Path(temporary_path).unlink(missing_ok=True)
                        removed = True
                        break
                    except PermissionError:
                        await asyncio.sleep(0.05)
                if not removed:
                    raise ReadOnlyValidationError(
                        "temporary validation database cleanup failed"
                    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    load_dotenv()
    selected = os.environ.get("KALSHI_ENVIRONMENT", "").strip().lower() or "unset"
    print(f"ENVIRONMENT={selected if selected in ENVIRONMENT_URLS else 'UNSET'}")
    for name, configured in credential_status(os.environ).items():
        print(f"{name}={'SET' if configured else 'UNSET'}")
    try:
        result = asyncio.run(run_read_only_validation())
    except Exception as exc:
        print(f"READ_ONLY_VALIDATION=BLOCKED ({type(exc).__name__})")
        return 2

    reconciliation = result["reconciliation"]
    health = result["health"]
    blockers = [check.reason for check in result["readiness"].blocking_checks]
    print(f"BALANCE={result['balance'] if result['balance'] is not None else 'UNAVAILABLE'}")
    print(f"POSITION_COUNT={result['position_count']}")
    print(f"ORDER_COUNT={result['order_count']}")
    print(f"FILL_COUNT={result['fill_count']}")
    print(f"MARKET_COUNT={result['market_count']}")
    print(f"ELIGIBLE_MARKET_COUNT={result['eligible_market_count']}")
    scan_stats = result.get("scan_stats", {})
    print(f"MARKETS_DISCOVERED={scan_stats.get('markets_discovered', 'UNAVAILABLE')}")
    print(f"MARKETS_WITHIN_72H={scan_stats.get('markets_within_72h', 'UNAVAILABLE')}")
    print(f"SPORTS_MARKETS_WITHIN_72H={scan_stats.get('sports_markets_within_72h', 'UNAVAILABLE')}")
    print(f"NON_SPORTS_MARKETS_WITHIN_72H={scan_stats.get('non_sports_markets_within_72h', 'UNAVAILABLE')}")
    print(f"RECONCILIATION_STATUS={reconciliation.status}")
    print(f"RECONCILIATION_HEALTH={'HEALTHY' if health['healthy_status'] and health['fresh'] else 'UNHEALTHY'}")
    print(f"CRITICAL_ALERTS={health['critical_alert_count']}")
    print(f"ALERTS={result['alert_count']}")
    print(f"MISMATCHES={reconciliation.mismatch_count}")
    print(f"READINESS_BLOCKERS={len(blockers)}")
    for index, reason in enumerate(blockers, start=1):
        print(f"BLOCKER_{index}={reason}")
    print(result["readiness"].format_summary())
    print("READ_ONLY_VALIDATION=COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

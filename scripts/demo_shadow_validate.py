#!/usr/bin/env python3
"""Run one authenticated, demo-only ingestion and shadow reconciliation cycle."""

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_URL = "https://external-api.demo.kalshi.co"


class DemoReadError(RuntimeError):
    """Secret-safe wrapper for failures from an authenticated read call."""


class ReadOnlyDemoClient:
    """Expose only the read operations required by ingestion and reconciliation."""

    environment = "demo"
    base_url = DEMO_URL

    def __init__(self, client: Any):
        self._client = client

    async def _read(self, method: str, *args, **kwargs):
        try:
            return await getattr(self._client, method)(*args, **kwargs)
        except Exception:
            raise DemoReadError("authenticated demo read failed") from None

    async def _make_authenticated_request(self, method: str, endpoint: str, **kwargs):
        if method.upper() != "GET":
            raise DemoReadError("demo-shadow permits GET requests only")
        return await self._read("_make_authenticated_request", method, endpoint, **kwargs)

    async def get_market(self, *args, **kwargs):
        return await self._read("get_market", *args, **kwargs)

    async def get_markets(self, *args, **kwargs):
        return await self._read("get_markets", *args, **kwargs)

    async def get_all_orders(self, *args, **kwargs):
        return await self._read("get_all_orders", *args, **kwargs)

    async def get_all_fills(self, *args, **kwargs):
        return await self._read("get_all_fills", *args, **kwargs)

    async def get_positions(self, *args, **kwargs):
        return await self._read("get_positions", *args, **kwargs)


def _is_set(value: Optional[str]) -> bool:
    return bool(value and value.strip())


def credential_status(environment: Mapping[str, str]) -> dict:
    key_path = environment.get("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
    return {
        "KALSHI_API_KEY": _is_set(environment.get("KALSHI_API_KEY")),
        "KALSHI_PRIVATE_KEY_FILE": Path(key_path).is_file(),
    }


def validate_demo_safety(environment: Mapping[str, str]) -> None:
    if environment.get("KALSHI_ENVIRONMENT", "").strip().lower() != "demo":
        raise RuntimeError("KALSHI_ENVIRONMENT must be exactly demo")
    if environment.get("AUTHORITATIVE_LIVE_EXECUTION_ENABLED", "false").lower() == "true":
        raise RuntimeError("authoritative execution must remain disabled")
    if environment.get("LIVE_ORDER_SUBMISSION_KILL_SWITCH", "true").lower() != "true":
        raise RuntimeError("live order submission kill switch must remain enabled")
    status = credential_status(environment)
    if not all(status.values()):
        raise RuntimeError("demo credentials are incomplete")


async def run_demo_shadow_validation(
    *,
    environment: Optional[Mapping[str, str]] = None,
    client_factory: Optional[Callable[[], Any]] = None,
    ingestion_runner: Optional[Callable[..., Any]] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Execute a bounded cycle that has no order or cancellation capability."""
    environment = environment or os.environ
    validate_demo_safety(environment)

    from src.clients.kalshi_client import KalshiClient
    from src.jobs.ingest import run_ingestion
    from src.orders.readiness import ReadinessContext, SubmissionReadinessEvaluator
    from src.orders.reconciler import OrderReconciler
    from src.orders.repository import OrderRepository
    from src.utils.database import DatabaseManager

    client = client_factory() if client_factory else KalshiClient(environment="demo")
    if getattr(client, "environment", None) != "demo" or getattr(client, "base_url", None) != DEMO_URL:
        await client.close()
        raise RuntimeError("Kalshi client is not pinned to the demo endpoint")

    temporary_path = None
    if db_path is None:
        descriptor, temporary_path = tempfile.mkstemp(prefix="kalshi-demo-shadow-", suffix=".db")
        os.close(descriptor)
        db_path = temporary_path

    read_client = ReadOnlyDemoClient(client)
    manager = DatabaseManager(db_path)
    repository = OrderRepository(db_path)
    queue = asyncio.Queue()
    ingestion_runner = ingestion_runner or run_ingestion
    try:
        await manager.initialize()
        await ingestion_runner(manager, queue, kalshi_client=read_client)
        reconciler = OrderReconciler(
            repository, read_client, shadow_mode=True, paper_mode=False,
            project_positions=False,
        )
        reconciliation = await reconciler.reconcile(trigger="demo_shadow", full=True)
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
                configured_environment="demo",
                client_environment="demo",
                reconciliation_max_age_seconds=30,
                total_markets=total_markets,
                eligible_markets=eligible_markets,
            ),
            scope="cycle",
        )
        health = await repository.get_reconciliation_health(30)
        return {
            "endpoint": DEMO_URL,
            "market_count": total_markets,
            "eligible_market_count": eligible_markets,
            "reconciliation": reconciliation,
            "health": health,
            "readiness": readiness,
        }
    finally:
        try:
            await client.close()
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
                    raise RuntimeError("temporary validation database cleanup failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    load_dotenv()
    status = credential_status(os.environ)
    for name, configured in status.items():
        print(f"{name}={'SET' if configured else 'UNSET'}")
    try:
        result = asyncio.run(run_demo_shadow_validation())
    except Exception as exc:
        print(f"DEMO_SHADOW_VALIDATION=BLOCKED ({type(exc).__name__})")
        return 2
    print("DEMO_ENDPOINT_VERIFIED=YES")
    print(f"MARKET_COUNT={result['market_count']}")
    print(f"ELIGIBLE_MARKETS={result['eligible_market_count']}")
    reconciliation = result["reconciliation"]
    print(f"RECONCILIATION_STATUS={reconciliation.status}")
    print(f"RECONCILIATION_ALERTS={result['health']['critical_alert_count']}")
    print(result["readiness"].format_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

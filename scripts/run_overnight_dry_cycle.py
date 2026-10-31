"""One bounded, offline paper execution used to validate overnight safety plumbing."""

import asyncio
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.jobs.execute import execute_position
from src.utils.database import DatabaseManager, Market, Position


class NoNetworkClient:
    def __getattr__(self, name):
        raise RuntimeError(f"offline dry cycle attempted exchange capability: {name}")


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kalshi-offline-dry-") as directory:
        manager = DatabaseManager(str(Path(directory) / "dry-cycle.db"))
        await manager.initialize()
        await manager.upsert_markets([Market(
            market_id="OFFLINE-DRY-CYCLE", title="Offline safety validation",
            yes_price=.40, no_price=.60, volume=1000, expiration_ts=2_000_000_000,
            category="diagnostic", status="active",
            last_updated=datetime.now(timezone.utc),
        )])
        position = Position(
            market_id="OFFLINE-DRY-CYCLE", side="YES", entry_price=.40,
            quantity=1, timestamp=datetime.now(timezone.utc), status="pending",
            strategy="overnight_dry_cycle",
        )
        position.id = await manager.add_position(position)
        success = await execute_position(position, False, manager, NoNetworkClient())
        stored = await manager.get_position_by_id(position.id)
        safe = bool(success and stored and not stored.live and stored.status == "open")
        print("DRY_RUN_MODE=PAPER")
        print("EXCHANGE_API_ATTEMPTED=false")
        print(f"PAPER_POSITION_RECORDED={'true' if safe else 'false'}")
        print(f"DRY_RUN_RESULT={'PASS' if safe else 'FAIL'}")
        return 0 if safe else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

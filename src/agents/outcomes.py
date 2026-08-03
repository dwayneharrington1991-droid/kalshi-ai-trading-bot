"""Idempotent outcome capture from supplied authoritative settlement records."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .repository import AgentRepository


class OutcomeResolutionService:
    def __init__(self, repository: AgentRepository):
        self.repository = repository

    async def resolve(self, settlements: Iterable[Mapping[str, Any]]) -> dict:
        updated = skipped = 0
        for record in settlements:
            market_id = record.get("market_id")
            status = str(record.get("status", "ambiguous")).lower()
            if not market_id or status not in {"settled", "voided", "canceled", "ambiguous"}:
                skipped += 1
                continue
            outcome = record.get("actual_outcome") if status == "settled" else None
            count = await self.repository.resolve_outcome(
                str(market_id), status, outcome, record.get("settlement_timestamp")
            )
            updated += count
        return {"updated": updated, "skipped": skipped}


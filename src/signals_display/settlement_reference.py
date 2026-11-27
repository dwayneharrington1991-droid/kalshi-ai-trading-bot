"""Timestamp-safe official/estimated settlement reference accumulator."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime

from .contract_rules import decimal_value
from .model import timestamp


@dataclass
class SettlementReference:
    window_start: datetime
    window_end: datetime
    interval_seconds: int
    authorized_official_source: bool = False
    source: str = "unavailable"
    maximum_clock_skew_seconds: float = 2.0
    ticks: dict[int, Decimal] = field(default_factory=dict)
    rejected: dict[str, int] = field(default_factory=lambda: {
        "out_of_window": 0, "clock_skew": 0, "duplicate_or_out_of_order": 0, "invalid": 0,
    })

    def add(self, event_at, received_at, value):
        try:
            event = event_at if isinstance(event_at, datetime) else timestamp(event_at)
            received = received_at if isinstance(received_at, datetime) else timestamp(received_at)
            if event.tzinfo is None or received.tzinfo is None:
                raise ValueError("timestamps must include timezone")
            price = decimal_value(value, low=Decimal("0.00000001"))
        except (ValueError, TypeError, AttributeError):
            self.rejected["invalid"] += 1
            return False
        if not self.window_start <= event < self.window_end:
            self.rejected["out_of_window"] += 1
            return False
        delay = (received - event).total_seconds()
        if delay < -self.maximum_clock_skew_seconds or delay > 30:
            self.rejected["clock_skew"] += 1
            return False
        bucket = int((event - self.window_start).total_seconds()) // self.interval_seconds
        if bucket in self.ticks:
            self.rejected["duplicate_or_out_of_order"] += 1
            return False
        self.ticks[bucket] = price
        return True

    def result(self):
        expected = int((self.window_end - self.window_start).total_seconds()) // self.interval_seconds
        complete = expected > 0 and len(self.ticks) == expected and set(self.ticks) == set(range(expected))
        value = sum(self.ticks.values(), Decimal(0)) / Decimal(expected) if complete else None
        return {
            "status": "COMPLETE" if complete else "INCOMPLETE",
            "mode": "OFFICIAL_REFERENCE" if self.authorized_official_source else "ESTIMATED_REFERENCE",
            "official": self.authorized_official_source,
            "source": self.source,
            "value": str(value) if value is not None else None,
            "received_ticks": len(self.ticks), "expected_ticks": expected,
            "missing_ticks": max(0, expected - len(self.ticks)), "rejected": dict(self.rejected),
            "warning": None if self.authorized_official_source else
                "External proxy observations are an estimate and are not the official settlement index.",
        }

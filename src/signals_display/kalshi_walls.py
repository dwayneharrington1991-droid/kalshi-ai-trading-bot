"""Descriptive Kalshi L2 wall persistence; never labels institutional intent."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .contract_rules import decimal_value


class WallTracker:
    def __init__(self, *, concentration_multiple=3, maximum_levels=100):
        self.multiple = decimal_value(concentration_multiple, low=Decimal(1))
        self.maximum_levels = maximum_levels
        self.previous = {"bid": {}, "ask": {}}
        self.persistence = defaultdict(int)

    def update(self, bids, asks):
        result = {"bid": [], "ask": []}
        current = {}
        for side, raw in (("bid", bids), ("ask", asks)):
            levels = [(decimal_value(p, low=Decimal(0), high=Decimal(1)),
                       decimal_value(q, low=Decimal(0))) for p, q in raw[:self.maximum_levels]]
            current[side] = dict(levels)
            positive = [q for _, q in levels if q > 0]
            baseline = sorted(positive)[len(positive) // 2] if positive else Decimal(0)
            for price, size in levels:
                key = (side, price)
                if baseline and size >= baseline * self.multiple:
                    self.persistence[key] += 1
                    result[side].append({"price": str(price), "size": str(size),
                        "relative_to_median": str(size / baseline),
                        "persistence_updates": self.persistence[key],
                        "interpretation": "Large displayed concentration; origin and predictive value unknown"})
        replenished = cancelled = migrated = Decimal(0)
        for side in ("bid", "ask"):
            old, new = self.previous[side], current[side]
            replenished += sum((max(Decimal(0), q - old.get(p, Decimal(0))) for p, q in new.items()), Decimal(0))
            cancelled += sum((max(Decimal(0), q - new.get(p, Decimal(0))) for p, q in old.items()), Decimal(0))
            migrated += Decimal(len(set(old) ^ set(new)))
        self.previous = current
        return {"status": "DESCRIPTIVE_UNVALIDATED", "walls": result,
                "replenished_quantity": str(replenished), "cancelled_quantity": str(cancelled),
                "depth_levels_changed": str(migrated), "institutional_claim": False,
                "actionable": False}


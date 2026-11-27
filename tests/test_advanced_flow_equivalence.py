"""The faster flow summary returns exactly what the original full scans returned.

The original filtered every retained record (up to 20,000 trades and 20,000
quotes per source) several times per update. Measured 2026-11-20: about 100 ms
per summary on a desktop for two sources, about 280 ms per update on the
single-CPU Oracle host. The replacement finds the same contiguous slices by
binary search. This test compares the complete outputs against a reference
copy of the original filtering on randomized streams with resets, delayed
trades, equal timestamps and historical cutoffs.
"""
import json
import random
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import advanced_flow as af

T0 = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


class Reference(af.AdvancedFlow):
    """The original list-comprehension filtering, kept verbatim for comparison."""

    @staticmethod
    def _eligible(records, cutoff):
        return [x for x in records if x["received"] <= cutoff and (x["event"] is None or x["event"] <= cutoff)]

    @staticmethod
    def _suffix_from(records, keep):
        return [x for x in records if keep(x)]

    @classmethod
    def _generation_block(cls, records, generation):
        return [x for x in records if x["generation"] == generation]


def _feed(flows, seed, n=3000):
    rng = random.Random(seed)
    price, session, t = 90000.0, "s0", 0.0
    for i in range(n):
        t += rng.choice([0.0, 0.0, 0.05, 0.2, 0.7, 1.5])          # equal receipts happen
        if rng.random() < 0.004:
            session = f"s{i}"                                     # reconnect
        if rng.random() < 0.002:
            t += 400                                              # long outage
        at = T0 + timedelta(seconds=t)
        price += rng.gauss(0, 3)
        src = rng.choice(["Coinbase BTC/USD stream", "Kraken BTC/USD stream"])
        lag = rng.choice([0.0, 0.01, 0.3, 2.0])
        if rng.random() < 0.6:
            obs = {"source": src, "kind": "trade", "price": price, "volume": rng.random() * 0.2,
                   "aggressor": rng.choice(["buy", "sell"]), "trade_id": f"{src}{i}",
                   "received_at": at.isoformat(), "event_at": (at - timedelta(seconds=lag)).isoformat(), "session_id": session}
        else:
            obs = {"source": src, "kind": "quote", "price": price, "bid": price - 0.5, "ask": price + 0.5,
                   "bid_size": rng.random(), "ask_size": rng.random(), "received_at": at.isoformat(),
                   "event_at": (at - timedelta(seconds=lag)).isoformat(), "session_id": session}
        for f in flows:
            try:
                f.observe(dict(obs))
            except ValueError:
                pass
    return T0 + timedelta(seconds=t)


@pytest.mark.parametrize("seed", range(6))
def test_summaries_are_identical_to_the_original_full_scans(seed):
    fast, slow = af.AdvancedFlow(max_records=800), Reference(max_records=800)
    end = _feed([fast, slow], seed)
    rng = random.Random(100 + seed)
    cutoffs = [end, end + timedelta(seconds=3), end + timedelta(seconds=30)] + [
        T0 + timedelta(seconds=rng.uniform(0, (end - T0).total_seconds())) for _ in range(12)]
    for cutoff in cutoffs:
        for threshold in (None, 90000.0):
            a = fast.summarize(cutoff, threshold=threshold)
            b = slow.summarize(cutoff, threshold=threshold)
            assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_closed_bars_unaffected():
    fast, slow = af.AdvancedFlow(), Reference()
    end = _feed([fast, slow], 9, n=1500)
    for src in fast.sources:
        assert fast.closed_bars(src, end) == slow.closed_bars(src, end)

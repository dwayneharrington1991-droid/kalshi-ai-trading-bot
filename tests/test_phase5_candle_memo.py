"""Exact-reuse caches must return exactly what the original functions return."""
import random
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import candle_memo
from src.signals_display.candles import _candle_context_uncached, candle_context
from src.signals_display.intelligence import _technical_context_uncached, technical_context
from src.signals_display.prediction_engine import _context24_uncached, context24

BASE = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def history(rng, end_minute, bars=1445, defect=None, received_offset=1.5):
    start0 = int(end_minute.timestamp()) - bars * 60
    candles, price = [], 100000.0
    for i in range(bars):
        price += rng.uniform(-30, 30)
        o, c = price, price + rng.uniform(-10, 10)
        candles.append({"start": start0 + 60 * i, "open": o, "high": max(o, c) + rng.uniform(0, 5),
                        "low": min(o, c) - rng.uniform(0, 5), "close": c, "volume": rng.uniform(0, 3)})
    if defect == "gap":
        del candles[rng.randrange(bars - 300, bars - 2)]
    elif defect == "duplicate":
        candles.append(dict(candles[rng.randrange(bars - 50, bars)]))
    elif defect == "unaligned_old":
        candles[3]["start"] += 7
    elif defect == "unaligned_recent":
        candles[-3]["start"] += 7
    elif defect == "bad_ohlc":
        candles[-10]["high"] = candles[-10]["low"] - 1
    elif defect == "bad_forming":
        candles[-1]["high"] = -5.0
    elif defect == "missing_volume":
        del candles[-20]["volume"]
    elif defect == "flat":
        for bar in candles:
            bar.update(open=1.0, high=1.0, low=1.0, close=1.0)
    received = end_minute + timedelta(seconds=received_offset)
    return {"source": "Coinbase test", "received_at": received.isoformat(), "candles": candles}


def clocks(end_minute, rng):
    base = end_minute.timestamp()
    offsets = [0, 0.5, 1.5, 1.6, 29.9, 59.999, 60, 60.001, 61.5, 89, 119.9, 120, 121.5, 149, 150,
               151.4, 151.6, 179.9, 180, 181, 300, -3]
    offsets += [rng.uniform(-5, 400) for _ in range(6)]
    return [datetime.fromtimestamp(base + o, timezone.utc) for o in offsets]


DEFECTS = [None, None, "gap", "duplicate", "unaligned_old", "unaligned_recent", "bad_ohlc",
           "bad_forming", "missing_volume", "flat"]


@pytest.mark.parametrize("seed", range(5))
def test_cached_functions_equal_originals_for_every_clock_and_defect(seed):
    rng = random.Random(seed)
    candle_memo.clear()
    for defect in DEFECTS:
        end_minute = BASE + timedelta(minutes=rng.randrange(0, 600))
        data = history(rng, end_minute, defect=defect, received_offset=rng.choice([0.2, 1.5, 59.9]))
        # Several passes so later calls are served from the warm cache.
        for _ in range(2):
            for now in clocks(end_minute, rng):
                assert candle_context(data, now) == _candle_context_uncached(data, now), (defect, now)
                assert context24(data, now) == _context24_uncached(data, now), (defect, now)
                assert technical_context(data, now) == _technical_context_uncached(data, now), (defect, now)
    assert candle_memo.stats["hits"] > 0


def test_same_content_in_a_new_object_hits_and_changed_content_misses():
    rng = random.Random(7)
    candle_memo.clear()
    data = history(rng, BASE)
    now = BASE + timedelta(seconds=5)
    first = context24(data, now)
    assert first["status"] == "READY"
    hits = candle_memo.stats["hits"]
    assert context24(deepcopy(data), now) == first
    assert candle_memo.stats["hits"] == hits + 1
    changed = deepcopy(data)
    changed["candles"][-30]["close"] += 0.01      # a repaired historical bar
    changed["candles"][-30]["high"] += 0.01
    assert context24(changed, now) == _context24_uncached(changed, now) != first


def test_caller_mutation_never_corrupts_the_cache():
    rng = random.Random(3)
    candle_memo.clear()
    data = history(rng, BASE)
    now = BASE + timedelta(seconds=5)
    for function, original in ((context24, _context24_uncached),
                               (technical_context, _technical_context_uncached)):
        value = function(data, now)
        value["features"]["poison"] = 1
        assert function(data, now) == original(data, now)
    value, _ = candle_context(data, now)
    value["closed_bars"] = -1
    assert candle_context(data, now) == _candle_context_uncached(data, now)


def test_invalid_clock_and_missing_data_behave_exactly_like_the_originals():
    rng = random.Random(1)
    data = history(rng, BASE)
    assert candle_context({}, BASE) == _candle_context_uncached({}, BASE)
    assert context24({}, BASE) == _context24_uncached({}, BASE)
    assert technical_context({}, BASE) == _technical_context_uncached({}, BASE)
    with pytest.raises(ValueError):
        technical_context(data, datetime(2026, 11, 18, 12, 0))   # naive clock: same error

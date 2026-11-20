"""Exact reuse of candle-window computations between prediction cycles.

The live prediction cycle re-validated and re-derived the same 1,440 one-minute
candles several times per cycle (candle_context twice, context24 and
technical_context), although the candle history only changes about once a
minute. On a shared single CPU this pure-Python work dominated the prediction
cycle and pushed published quotes past the 5-second freshness limit.

Correctness contract (no value or decision may change):

* A cache entry is keyed by the full CONTENT of the candle history (source,
  receipt time and every bar's start/open/high/low/close/volume), never by
  object identity, plus the exact quantity through which the function's clock
  argument selects bars (an integer minute index or the context24 cutoff).
* Only successful results are cached. Every error path calls the original
  function, so error text and precedence are unchanged.
* Every clock-dependent freshness check is re-evaluated on every call, in the
  original order, before a cached value is returned; any doubt falls back to
  the original function.
* Callers receive an independent deep copy; nothing they do can alter a cache
  entry.
"""
from collections import OrderedDict
from copy import deepcopy
from operator import itemgetter
import threading

_KEYS = ("start", "open", "high", "low", "close", "volume")
_ROW = itemgetter(*_KEYS)
_MAX_ENTRIES = 32
_lock = threading.Lock()
_entries = OrderedDict()
stats = {"hits": 0, "misses": 0, "bypassed": 0}


def fingerprint(data):
    """Hashable content identity of a candle response, or None (do not cache)."""
    try:
        rows = tuple(map(_ROW, data["candles"]))
        key = (data.get("source"), data.get("received_at"), rows)
        hash(key)
        return key
    except Exception:
        return None


def aligned_starts(fp):
    """Integer starts if every bar is minute-aligned, else None (do not cache)."""
    try:
        starts = [int(row[0]) for row in fp[2]]
    except (TypeError, ValueError, OverflowError):
        return None
    return starts if all(start % 60 == 0 for start in starts) else None


def get(key):
    with _lock:
        value = _entries.get(key)
        if value is None:
            stats["misses"] += 1
            return None
        _entries.move_to_end(key)
        stats["hits"] += 1
        return value


def put(key, value):
    with _lock:
        _entries[key] = value
        _entries.move_to_end(key)
        while len(_entries) > _MAX_ENTRIES:
            _entries.popitem(last=False)


def bypass():
    with _lock:
        stats["bypassed"] += 1


def clear():
    with _lock:
        _entries.clear()
        for name in stats:
            stats[name] = 0


def copy(value):
    return deepcopy(value)

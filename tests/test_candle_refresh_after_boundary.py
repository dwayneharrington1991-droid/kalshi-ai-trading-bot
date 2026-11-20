"""A candle refresh that lands before the just-closed minute is published retries soon.

Found 2026-11-20: in 4 of 442 openings the frozen 24 h history ended 120 s before
the open instead of 60 s. Each time the history had been fetched exactly at the
minute boundary 60 s before the open, before Coinbase published the minute that
had just closed; the cache still counted as complete, so the next refresh came
60 s later, after the opening had frozen. Only closed bars are ever used.
"""
from datetime import datetime, timedelta, timezone

from src.signals_display.monitor import PublicFeed

B = datetime(2026, 11, 20, 17, 44, tzinfo=timezone.utc)          # a minute boundary (open at 17:45)


def _feed(last_bar_end):
    feed = PublicFeed(lambda *_a, **_k: None)
    start = int(last_bar_end.timestamp()) - 60 * 1440
    feed.candle_cache = {"candles": [{"start": str(start + 60 * i)} for i in range(1440)]}
    return feed


def test_stale_by_one_closed_minute_retries_in_five_seconds():
    feed = _feed(B - timedelta(minutes=1))                          # newest bar ends at 17:43
    assert feed._candle_retry_seconds(1440, B + timedelta(seconds=2)) == 5


def test_current_history_keeps_the_one_minute_cadence():
    feed = _feed(B)                                                 # newest bar ends at 17:44
    assert feed._candle_retry_seconds(1440, B + timedelta(seconds=2)) == 60
    assert feed._candle_retry_seconds(1440) == 60                   # callers without a clock: unchanged

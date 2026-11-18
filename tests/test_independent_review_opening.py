"""Independent review (2026-11-18): opening prediction leakage, immutability
and abstention scoring. Written separately from the implementation's own
tests; every case perturbs ONLY information that must not matter (or must)
and checks the opening decision bit-for-bit."""
import json
import math
from copy import deepcopy
from datetime import timedelta

import pytest

from src.signals_display import prediction_engine as engine
from src.signals_display.monitor import Journal
from tests.test_signals_display import NOW
from tests.test_signals_prediction_engine import history, market, points


def _forecast(hist, pts, generated=None):
    return engine.opening_forecast(market(), hist, pts, generated or NOW + timedelta(seconds=1),
                                   reference_source="test reference")


def _decision(result):
    return {k: result.get(k) for k in ("up", "down", "direction", "reason")}


def test_baseline_produces_a_decision_for_the_leakage_tests_to_protect():
    result = _forecast(history(), points())
    assert result.get("up") is not None, result.get("reason")


def test_bars_starting_at_or_after_the_opening_never_change_the_decision():
    base = _forecast(history(), points())
    poisoned = history()
    opened = int(NOW.timestamp())
    for i in range(30):                                   # absurd post-open bars
        start = opened + 60 * i
        poisoned["candles"].append(dict(start=start, open=1e6, high=1e6 + 1, low=1e6 - 1,
                                        close=1e6 * (1 + i % 2), volume=9999))
    assert _decision(_forecast(poisoned, points())) == _decision(base)


def test_reference_receipts_after_the_opening_never_change_the_decision():
    base = _forecast(history(), points())
    pts = points() + [(NOW + timedelta(seconds=s), 1e6 * (1 + s % 2)) for s in range(1, 120)]
    assert _decision(_forecast(history(), pts)) == _decision(base)


def test_a_history_received_after_the_opening_is_rejected_not_used():
    late = history(received=NOW + timedelta(seconds=5))
    result = _forecast(late, points())
    assert result.get("up") is None and result.get("direction") in {None, "WAIT", "UNCLEAR"}


def test_delayed_generation_changes_timing_metadata_only():
    on_time = _forecast(history(), points(), NOW + timedelta(seconds=1))
    delayed = _forecast(history(), points(), NOW + timedelta(seconds=110))
    assert _decision(delayed) == _decision(on_time)


def test_pre_open_history_does_matter_so_the_leakage_tests_are_not_vacuous():
    base = _forecast(history(), points())
    shifted = history()
    for bar in shifted["candles"][-30:]:
        for k in ("open", "high", "low", "close"):
            bar[k] += 400
    assert _forecast(shifted, points()).get("up") != base.get("up")


def test_opening_records_are_immutable_and_first_write_wins(tmp_path):
    journal = Journal(tmp_path / "j.db")
    m = market()
    first = _forecast(history(), points())
    first.update(closes_at=m["closes_at"])
    stored = journal.opening_record(m["ticker"], "PUBLIC MARKET DATA", first, NOW + timedelta(seconds=1))
    other = deepcopy(first)
    other.update(up=1 - first["up"], direction="DOWN" if first.get("direction") == "UP" else "UP")
    again = journal.opening_record(m["ticker"], "PUBLIC MARKET DATA", other, NOW + timedelta(seconds=50))
    assert again["up"] == stored["up"] and again.get("direction") == stored.get("direction")
    with journal.connect() as db, pytest.raises(Exception, match="immutable"):
        db.execute("UPDATE btc_engine_opening SET up=0.01")
    with journal.connect() as db, pytest.raises(Exception, match="immutable"):
        db.execute("DELETE FROM btc_engine_opening")


@pytest.mark.parametrize("saved,outcome,expected", [
    ({"direction": "WAIT", "up": 0.52, "decision_policy": {"threshold": 0.55}}, 0, "WAIT"),
    ({"direction": "UNCLEAR", "up": None}, 1, "UNCLEAR"),
    ({"direction": "UP", "up": 0.61, "decision_policy": {"threshold": 0.55}}, 1, "UP"),
])
def test_abstentions_are_preserved_and_never_scored_as_wrong(saved, outcome, expected):
    assert engine.recorded_opening_direction(saved) == expected


def test_verified_scoreboard_excludes_wait_calls_from_accuracy(tmp_path):
    journal = Journal(tmp_path / "j.db")
    rows = [("KX-A", "WAIT", 0.53, "CALIBRATED", 0), ("KX-B", "UP", 0.64, "CALIBRATED", 1),
            ("KX-C", "DOWN", 0.36, "CALIBRATED", 1), ("KX-D", "UP", 0.58, "UNCALIBRATED", 1)]
    with journal.connect() as db:
        for i, (ticker, direction, up, status, outcome) in enumerate(rows):
            payload = {"direction": direction, "up": up, "confidence": {"status": status}}
            at = (NOW + timedelta(minutes=15 * i)).isoformat()
            cols = [r[1] for r in db.execute("PRAGMA table_info(btc_engine_opening)")]
            values = {"mode": "PUBLIC MARKET DATA", "ticker": ticker, "generated_at": at, "up": up,
                      "payload": json.dumps(payload), "closes_at": at, "cutoff": at, "opens_at": at,
                      "version": "test", "direction": direction}
            use = [c for c in cols if c in values]
            missing = [c for c in cols if c not in values]
            vals = [values[c] for c in use] + [None] * len(missing)
            db.execute(f"INSERT INTO btc_engine_opening ({','.join(use + missing)}) VALUES ({','.join('?' * len(cols))})", vals)
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", (ticker, "official", at, outcome))
    board = journal.verified_opening_scoreboard()
    assert board["verified_calls"] == 2 and board["correct"] == 1 and board["wrong"] == 1
    assert board["excluded"]["wait_or_no_call"] == 2       # the WAIT and the uncalibrated call


# ------------------------------------------------- 1438/1440 candle backoff
def test_confirmed_empty_minute_is_not_rerequested_every_five_seconds(monkeypatch):
    from datetime import datetime, timezone
    from urllib.parse import parse_qs, urlsplit
    import httpx
    from src.signals_display import monitor as module
    from src.signals_display.monitor import PublicFeed

    cutoff = int(NOW.timestamp()) // 60 * 60
    absent = cutoff - 300
    trade_calls = []
    clock = [NOW]

    def get(url, **_kwargs):
        query = parse_qs(urlsplit(url).query)
        if url.startswith(module.COINBASE_MARKET_TRADES):
            trade_calls.append(url)
            return httpx.Response(200, json={"trades": []}, request=httpx.Request("GET", url))
        if "api.exchange.coinbase.com" in url:
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))
        left, right = int(query["start"][0]), int(query["end"][0])
        return httpx.Response(200, json={"candles": [
            dict(start=str(t), open="100", high="102", low="99", close="101", volume="1")
            for t in range(left, right, 60) if t != absent]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    feed = PublicFeed(get)
    first = feed.candles(clock[0])
    assert first["diagnostics"]["status"] == "INCOMPLETE" and len(trade_calls) == 1
    assert {"start": absent, "status": "NO_VERIFIED_TRADES"} in first["diagnostics"]["trade_recovery"]
    assert all(bar["start"] != absent for bar in first["candles"])          # nothing invented
    assert feed._candle_retry_seconds(len(first["candles"])) == 60           # no 5 s loop

    clock[0] = NOW + timedelta(seconds=61)
    second = feed.candles(clock[0])
    assert len(trade_calls) == 1                                             # not re-requested
    assert {"start": absent, "status": "CONFIRMED_NO_TRADES_RECHECK_LATER"} in second["diagnostics"]["trade_recovery"]
    assert second["diagnostics"]["status"] == "INCOMPLETE"

    clock[0] = NOW + timedelta(seconds=61 + 600)
    feed.candles(clock[0])
    assert len(trade_calls) == 2                                             # re-checked after 10 min


def test_unconfirmed_gap_keeps_fast_retry(monkeypatch):
    from src.signals_display.monitor import PublicFeed
    feed = PublicFeed(lambda *a, **k: None)
    feed.candle_cache = {"candles": [{}] * 1439, "error": "MISSING_CANDLES"}
    assert feed._candle_retry_seconds(1439) == 5

"""Regression coverage for the preview-8768 minute-boundary failure."""
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display.prediction_engine import context24
from src.signals_display.server import candle_integrity_report


NOW = datetime(2026, 11, 17, 4, 0, 7, tzinfo=timezone.utc)
CUTOFF = int(NOW.timestamp()) // 60 * 60


def snapshot(*, cutoff=CUTOFF, count=1440, missing=(), duplicate=None):
    starts = list(range(cutoff - count * 60, cutoff, 60))
    starts = [stamp for stamp in starts if stamp not in set(missing)]
    if duplicate is not None:
        starts.append(duplicate)
    return {
        "window_cutoff": cutoff,
        "received_at": datetime.fromtimestamp(cutoff + 7, timezone.utc).isoformat(),
        "candles": [
            {"start": stamp, "open": 10, "high": 11, "low": 9,
             "close": 10.4 + (index % 3) / 10, "volume": 2}
            for index, stamp in enumerate(starts)
        ],
        "diagnostics": {"status": "READY"},
    }


@pytest.mark.parametrize("count", [1438, 1439, 1440])
def test_integrity_counts_exact_snapshot_window(count):
    report = candle_integrity_report(snapshot(count=count), NOW)
    assert report["bars"] == count
    assert len(report["missing_intervals"]) == 1440 - count
    assert report["status"] == ("READY" if count == 1440 else "INCOMPLETE")


@pytest.mark.parametrize("offset", [1440, 720, 1])
def test_integrity_reports_exact_first_middle_and_final_gap(offset):
    missing = CUTOFF - offset * 60
    report = candle_integrity_report(snapshot(missing=[missing]), NOW)
    assert report["status"] == "INCOMPLETE"
    assert report["missing_intervals"] == [missing]


def test_minute_rollover_does_not_reinterpret_previous_published_snapshot():
    source = snapshot()
    # This is the production race: HTTP wall time enters the next minute before
    # the collector publishes its next batch. The old snapshot remains complete
    # for its declared cutoff rather than falsely becoming 1439/1440.
    after_rollover = NOW + timedelta(minutes=1)
    report = candle_integrity_report(source, after_rollover)
    assert report["status"] == "READY"
    assert report["cutoff"] == CUTOFF
    assert report["wall_clock_cutoff"] == CUTOFF + 60
    assert report["snapshot_age_seconds"] == 60
    assert report["missing_intervals"] == []


def test_delayed_snapshot_eventually_becomes_stale_not_falsely_incomplete():
    report = candle_integrity_report(snapshot(), NOW + timedelta(minutes=3))
    assert report["status"] == "STALE"
    assert report["missing_intervals"] == []
    assert report["snapshot_age_seconds"] == 180


def test_next_collector_snapshot_truthfully_reports_delayed_final_candle():
    next_cutoff = CUTOFF + 60
    missing = next_cutoff - 60
    report = candle_integrity_report(
        snapshot(cutoff=next_cutoff, missing=[missing]),
        NOW + timedelta(minutes=1),
    )
    assert report["status"] == "INCOMPLETE"
    assert report["missing_intervals"] == [missing]


def test_duplicate_interval_cannot_pass_integrity_even_when_values_match():
    duplicate = CUTOFF - 60
    report = candle_integrity_report(snapshot(duplicate=duplicate), NOW)
    assert report["status"] == "INCOMPLETE"
    assert report["duplicate_bars"] == 1
    assert report["unique_bars"] == 1440


def test_out_of_order_and_future_intervals_cannot_pass_integrity():
    out_of_order = snapshot()
    out_of_order["candles"][-2:] = reversed(out_of_order["candles"][-2:])
    report = candle_integrity_report(out_of_order, NOW)
    assert report["status"] == "INCOMPLETE"
    assert report["out_of_order"] is True

    future = snapshot()
    future["candles"][-1]["start"] = CUTOFF
    report = candle_integrity_report(future, NOW)
    assert report["status"] == "INCOMPLETE"
    assert report["unexpected_intervals"] == [CUTOFF]
    assert report["missing_intervals"] == [CUTOFF - 60]


def test_incomplete_history_withholds_prediction_features_until_recovery():
    source = snapshot(missing=[CUTOFF - 60])
    incomplete = context24(source, NOW)
    assert incomplete["status"] == "INSUFFICIENT DATA"
    assert "1439/1440" in incomplete["reason"]
    source["candles"].append(
        {"start": CUTOFF - 60, "open": 10, "high": 11, "low": 9,
         "close": 10.6, "volume": 2}
    )
    recovered = context24(source, NOW)
    assert recovered["status"] == "READY"


def test_legacy_snapshot_uses_receipt_cutoff_without_wall_clock_race():
    source = snapshot()
    source.pop("window_cutoff")
    report = candle_integrity_report(source, NOW + timedelta(minutes=1))
    assert report["status"] == "READY"
    assert report["cutoff_source"] == "received_at_compatibility_fallback"


# --- Integration coverage: Claude a0fd7fc x Codex 3251994 -----------------


def test_receipt_older_than_opening_freshness_limit_is_stale_even_within_cutoff_age():
    # Cutoff age is only 120 s by minute arithmetic, but the receipt itself
    # is 172 s old: older than the 150 s context24 receipt limit.
    source = snapshot()
    report = candle_integrity_report(source, NOW + timedelta(seconds=172))
    assert report["snapshot_age_seconds"] == 120
    assert report["status"] == "STALE"
    assert any("receipt" in reason for reason in report["stale_reasons"])


def test_snapshot_without_receipt_cannot_claim_ready():
    source = snapshot()
    source.pop("received_at")
    report = candle_integrity_report(source, NOW)
    assert report["status"] == "STALE"
    assert report["stale_reasons"] == ["no receipt timestamp"]


def test_stale_takes_precedence_over_incomplete():
    report = candle_integrity_report(snapshot(missing=[CUTOFF - 60]), NOW + timedelta(minutes=5))
    assert report["status"] == "STALE"
    assert report["missing_intervals"] == [CUTOFF - 60]


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s.update(window_cutoff=CUTOFF + 60), "later than the request clock"),
    (lambda s: s.update(window_cutoff=CUTOFF + 30), "not minute-aligned"),
    (lambda s: s.update(received_at=(NOW + timedelta(minutes=2)).isoformat(),
                        window_cutoff=CUTOFF), "received_at is in the future"),
    (lambda s: s.update(window_cutoff=CUTOFF - 86460), "more than 24 h old"),
])
def test_impossible_snapshot_timestamps_are_invalid_never_ready(mutate, reason):
    source = snapshot()
    mutate(source)
    report = candle_integrity_report(source, NOW)
    assert report["status"] == "INVALID"
    assert any(reason in r for r in report["invalid_reasons"])
    assert report["snapshot_age_seconds"] is None


def test_window_cutoff_after_its_own_receipt_is_future_leakage():
    # Collector claims the window through 04:01 but received the data at 04:00:07.
    source = snapshot(cutoff=CUTOFF + 60)
    source["received_at"] = NOW.isoformat()
    report = candle_integrity_report(source, NOW + timedelta(minutes=1))
    assert report["status"] == "INVALID"
    assert "window_cutoff is later than its receipt" in report["invalid_reasons"]


def test_out_of_order_detected_before_sorting_even_when_set_is_complete():
    source = snapshot()
    source["candles"][0], source["candles"][700] = source["candles"][700], source["candles"][0]
    report = candle_integrity_report(source, NOW)
    assert report["missing_intervals"] == [] and report["unique_bars"] == 1440
    assert report["out_of_order"] is True and report["status"] == "INCOMPLETE"


def test_report_is_a_pure_function_of_one_snapshot():
    source = snapshot()
    before = repr(source)
    candle_integrity_report(source, NOW)
    assert repr(source) == before  # never mutates or repairs the publication


def test_endpoint_reads_bars_and_diagnostics_from_one_atomic_publication(tmp_path, monkeypatch):
    import json
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen

    import src.signals_display.server as server_module
    from src.signals_display.monitor import Journal
    from src.signals_display.server import Monitor, make_handler

    monitor = Monitor(None, Journal(tmp_path / "atomic.db"))
    first = snapshot()
    first["diagnostics"] = {"status": "READY", "cycle": 1}
    monitor.latest_candles = first
    monkeypatch.setattr(server_module, "utcnow", lambda: NOW)
    held = []

    class SpyLock:
        def __enter__(self):
            held.append(True)
            return self

        def __exit__(self, *exc):
            # A replacement published after the copy cannot leak into the report.
            replacement = snapshot(missing=[CUTOFF - 60])
            replacement["diagnostics"] = {"status": "INCOMPLETE", "cycle": 2}
            monitor.latest_candles = replacement
            return False

    monitor.candle_lock = SpyLock()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/candle-integrity", timeout=5) as r:
            report = json.load(r)
    finally:
        server.shutdown()
    assert held
    assert report["status"] == "READY" and report["diagnostics"]["cycle"] == 1
    assert report["missing_intervals"] == []

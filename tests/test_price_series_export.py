import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import price_series_export as pse
from src.signals_display import thirty_second_research as tsr

BASE = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _journal(path, rows, candles=()):
    """A temporary journal with the production table definitions for these two tables."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE feed_observations (source TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(source,timestamp))")
    db.execute("CREATE TABLE btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT,PRIMARY KEY(source,start))")
    for at, price in rows:
        payload = {"status": "CONNECTED", "data": {"price": price, "observed_at": _iso(at)}}
        db.execute("INSERT INTO feed_observations VALUES (?,?,?)",
                   (pse.PRICE_SOURCE, _iso(at), json.dumps(payload)))
    for start in candles:
        db.execute("INSERT INTO btc_candles VALUES (?,?,?,?)",
                   ("Coinbase BTC-USD", start, json.dumps({"start": start, "close": 1.0, "volume": 2.0}), _iso(start)))
    db.commit()
    db.close()
    return path


def _points(seed, n, spacing_range=(5, 40)):
    rng = random.Random(seed)
    t = BASE.timestamp()
    price = 100000.0
    out = []
    for _ in range(n):
        t += rng.uniform(*spacing_range)
        price *= 1 + rng.gauss(0, 0.0005)
        out.append((t, round(price, 2)))
    return out


def test_adapter_opens_the_journal_read_only(tmp_path):
    path = _journal(tmp_path / "j.db", _points(1, 5))
    db = pse.open_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO feed_observations VALUES ('x','y','z')")
    finally:
        db.close()


def test_grid_price_is_causal_later_observations_do_not_change_earlier_points():
    points = _points(2, 400)
    segments, _ = pse.grid_segments(points)
    spike = [(points[-1][0] + 5000, 999999.0)]           # a far-future observation
    segments_after, _ = pse.grid_segments(points + spike)
    flat = [b for s in segments for b in s]
    flat_after = [b for s in segments_after for b in s]
    cutoff = points[-1][0]
    before = [b for b in flat if b["ts"] <= cutoff]
    same = [b for b in flat_after if b["ts"] <= cutoff]
    assert before == same


def test_a_stale_gap_breaks_the_segment_and_is_never_filled():
    first = _points(3, 120, spacing_range=(5, 10))
    gap_start = first[-1][0]
    second = [(gap_start + 400 + i * 7, 100000.0 + i) for i in range(120)]
    segments, stats = pse.grid_segments(first + second, max_stale=60)
    assert stats["segments"] >= 2
    for seg in segments:
        for a, b in zip(seg, seg[1:]):
            assert b["ts"] - a["ts"] == pse.STEP_S
            assert b["ts"] - a["ts"] <= pse.STEP_S
    assert stats["stale_points"] > 0


def test_labels_never_cross_a_segment_boundary():
    segments = [
        [{"ts": 0, "close": 1.0, "volume": None}, {"ts": 30, "close": 2.0, "volume": None}],
        [{"ts": 300, "close": 3.0, "volume": None}, {"ts": 330, "close": 2.0, "volume": None}],
    ]
    X, y = tsr.segment_rows(segments)
    assert y == [1, None, 0, None]
    assert len(X) == 4


def test_non_positive_or_undated_rows_are_counted_not_used(tmp_path):
    path = tmp_path / "j.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE feed_observations (source TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(source,timestamp))")
    rows = [
        (pse.PRICE_SOURCE, "a", json.dumps({"data": {"price": 0, "observed_at": _iso(BASE.timestamp())}})),
        (pse.PRICE_SOURCE, "b", json.dumps({"data": {"price": 5.0}})),
        (pse.PRICE_SOURCE, "c", "not json"),
        (pse.PRICE_SOURCE, "d", json.dumps({"data": {"price": 7.0, "observed_at": _iso(BASE.timestamp() + 10)}})),
    ]
    db.executemany("INSERT INTO feed_observations VALUES (?,?,?)", rows)
    db.commit()
    db.close()
    ro = pse.open_readonly(path)
    points, skipped = pse.price_points(ro)
    ro.close()
    assert points == [(BASE.timestamp() + 10, 7.0)]
    assert skipped == 3


def test_candle_coverage_is_reported_but_not_used_as_a_30_second_price(tmp_path):
    path = _journal(tmp_path / "j.db", _points(4, 50), candles=[1000, 1060, 1120])
    report = pse.export(path)
    assert report["candles_60s"]["bars"] == 3
    assert report["candles_60s"]["bar_seconds"] == 60
    assert report["volume_available"] is False


def test_end_to_end_export_reports_spacing_grid_and_a_verdict(tmp_path):
    path = _journal(tmp_path / "j.db", _points(5, 3000))
    report = pse.export(path)
    assert report["spacing"]["observations"] == 3000
    assert report["grid"]["usable_points"] > 0
    assert report["research"]["verdict"] in ("NO_VALIDATED_EDGE", "VALIDATED_CANDIDATE_NEEDS_LIVE_SHADOW")
    assert "median_gap_s" in report["spacing"]


def test_export_of_an_empty_journal_refuses_cleanly(tmp_path):
    path = _journal(tmp_path / "j.db", [])
    report = pse.export(path)
    assert report["research"]["verdict"] == "NO_VALIDATED_EDGE"
    assert report["grid"]["usable_points"] == 0


def test_cli_refuses_to_overwrite_an_existing_report(tmp_path):
    path = _journal(tmp_path / "j.db", _points(6, 200))
    out = tmp_path / "report.json"
    out.write_text("keep")
    with pytest.raises(FileExistsError):
        pse._main(["--journal", str(path), "--out", str(out)])
    assert out.read_text() == "keep"

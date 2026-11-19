import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import price_series_export as pse

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "oracle" / "research_export_8768.py"
SOURCE = "Coinbase BTC/USD stream"
T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def _journal(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE feed_observations (source TEXT,timestamp TEXT,payload TEXT,PRIMARY KEY(source,timestamp))")
    db.executemany("INSERT INTO feed_observations VALUES (?,?,?)", rows)
    db.commit()
    db.close()
    return path


def _row(observed, price, *, event=None, received=None, bid=None, ask=None, bsz=1.0, asz=1.0):
    data = {"price": price, "observed_at": iso(observed), "bid": bid if bid is not None else price - 1,
            "ask": ask if ask is not None else price + 1, "bid_size": bsz, "ask_size": asz}
    if event is not None:
        data["event_time"] = iso(event)
    if received is not None:
        data["received_at"] = iso(received)
    return (SOURCE, iso(observed), json.dumps({"source": SOURCE, "data": data}))


def _run(db, out, since, until):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "--db", str(db), "--since", iso(since),
                           "--until", iso(until), "--out-dir", str(out)], capture_output=True, text=True)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_export_counts_duplicates_bad_rows_and_never_writes_the_journal(tmp_path):
    rows = [
        _row(T0 + timedelta(seconds=0), 100000.0, event=T0 - timedelta(seconds=2), received=T0 + timedelta(seconds=0)),
        _row(T0 + timedelta(seconds=5), 100000.0, event=T0 - timedelta(seconds=2), received=T0 + timedelta(seconds=5)),
        _row(T0 + timedelta(seconds=10), 100010.0, event=T0 + timedelta(seconds=9), received=T0 + timedelta(seconds=10)),
        _row(T0 + timedelta(seconds=11), 0.0, event=T0 + timedelta(seconds=11), received=T0 + timedelta(seconds=11)),
        _row(T0 + timedelta(seconds=12), 100020.0, bid=100030.0, ask=100010.0),
        (SOURCE, iso(T0 + timedelta(seconds=13)), "not json"),
        _row(T0 + timedelta(seconds=40), 100050.0, event=T0 + timedelta(seconds=38), received=T0 + timedelta(seconds=40)),
    ]
    db = _journal(tmp_path / "j.db", rows)
    before = _sha(db)
    out = tmp_path / "export"
    result = _run(db, out, T0 - timedelta(minutes=1), T0 + timedelta(minutes=5))
    assert result.returncode == 0, result.stderr
    assert _sha(db) == before                       # the journal is byte-identical after the export
    summary = json.loads((out / "summary.json").read_text())
    assert summary["rejected"] == {"non_positive_price": 1, "crossed_quote": 1, "unparseable": 1}
    assert summary["duplicates_identical_removed"] == 1      # second row repeats the first price and event
    assert summary["distinct_updates_exported"] == 3
    assert summary["receipt_field_present"] == 3
    assert (out / "coinbase_updates.csv").exists()
    assert _sha(out / "coinbase_updates.csv") == summary["csv_sha256"]


def test_availability_is_receipt_based_not_exchange_event_time(tmp_path):
    late_receipt = T0 + timedelta(seconds=50)
    rows = [
        _row(T0, 100000.0, event=T0 - timedelta(seconds=30), received=T0),
        # exchange event 2 s BEFORE the grid point at 60 s, but received only at 50 s, so it is not usable at 40 s
        _row(T0 + timedelta(seconds=20), 200000.0, event=T0 + timedelta(seconds=18), received=late_receipt),
        # a later update so the grid extends past the 60 s point (the grid ends at the last observation)
        _row(T0 + timedelta(seconds=95), 200500.0, event=T0 + timedelta(seconds=94), received=T0 + timedelta(seconds=95)),
    ]
    db = _journal(tmp_path / "j.db", rows)
    out = tmp_path / "export"
    assert _run(db, out, T0 - timedelta(minutes=1), T0 + timedelta(minutes=5)).returncode == 0
    points = pse.points_from_export_csv(out / "coinbase_updates.csv")
    segments, _ = pse.grid_segments(points, step=30)
    values = {b["ts"]: b["close"] for seg in segments for b in seg}
    e = int(T0.timestamp())
    assert values[e + 30] == 100000.0          # at 30 s the 200000 update has not been received yet (received at 50 s)
    assert values[e + 60] == 200000.0          # usable from its receipt time onward
    assert all(p != 200000.0 or t >= late_receipt.timestamp() for t, p in points)


def test_refuses_an_existing_output_folder_and_a_window_over_14_days(tmp_path):
    db = _journal(tmp_path / "j.db", [_row(T0, 100000.0, received=T0)])
    out = tmp_path / "export"
    out.mkdir()
    assert _run(db, out, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1)).returncode != 0
    assert _run(db, tmp_path / "x", T0 - timedelta(days=20), T0).returncode != 0


def test_row_cap_refuses_instead_of_exporting_everything(tmp_path):
    rows = [_row(T0 + timedelta(seconds=i), 100000.0 + i, received=T0 + timedelta(seconds=i)) for i in range(50)]
    db = _journal(tmp_path / "j.db", rows)
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--db", str(db), "--since", iso(T0 - timedelta(minutes=1)),
                             "--until", iso(T0 + timedelta(minutes=5)), "--out-dir", str(tmp_path / "e"),
                             "--max-rows", "10"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "exceeds the cap" in result.stderr
    assert not (tmp_path / "e").exists()


def test_export_csv_contains_only_the_needed_columns(tmp_path):
    db = _journal(tmp_path / "j.db", [_row(T0, 100000.0, event=T0, received=T0)])
    out = tmp_path / "export"
    assert _run(db, out, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1)).returncode == 0
    header = (out / "coinbase_updates.csv").read_text().splitlines()[0].split(",")
    assert header == ["available_at_utc", "event_at_utc", "price", "bid", "ask", "bid_size", "ask_size",
                      "source", "row_timestamp_utc"]

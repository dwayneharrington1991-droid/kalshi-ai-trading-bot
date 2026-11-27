"""Order-flow forward test OF1: prospective spec rules and point-in-time export."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import export_orderflow_records as ex  # noqa: E402
import research_order_flow_value as rv  # noqa: E402


def _spec(tmp_path, **kw):
    s = {"target": "first6", "prospective_start": "2026-11-27T03:15:00Z", "eval_start": "2026-11-28T00:00:00Z",
         "min_train": 50, "threshold": 0.55, "l2": 1.0, "candidates": {"price_only": ["price.return_60s"]}}
    s.update(kw)
    p = tmp_path / "spec.json"; p.write_text(json.dumps(s)); return p


def test_prospective_spec_rules(tmp_path):
    spec = rv._load_spec(_spec(tmp_path))
    assert spec["prospective_start"].isoformat().startswith("2026-11-27T03:15")
    with pytest.raises(ValueError):
        rv._load_spec(_spec(tmp_path, prospective_start="2026-11-19T00:00:00Z"))     # inside the development period
    with pytest.raises(ValueError):
        rv._load_spec(_spec(tmp_path, eval_start="2026-11-27T00:00:00Z"))            # evaluation before registration


def test_records_before_registration_are_rejected(tmp_path):
    spec = rv._load_spec(_spec(tmp_path))
    rec = {"sample_id": "A", "target": "first6", "decision_at": "2026-11-27T03:00:00Z",
           "label_observed_at": "2026-11-27T03:07:00Z", "label": "UP", "baseline_probability": 0.5,
           "baseline_generated_at": "2026-11-27T03:00:00Z",
           "families": {"price": {"received_at": "2026-11-27T02:59:59Z", "values": {"return_60s": 0.0}}}}
    f = tmp_path / "r.jsonl"; f.write_text(json.dumps(rec) + "\n")
    with pytest.raises(ValueError, match="prospective start"):
        rv.load_records(f, spec)


def _flow_db(path, start, seconds, live=True):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE book_seconds(ts REAL, venue TEXT, status TEXT, mid REAL, spread REAL, bid_025 REAL,
        ask_025 REAL, imb_010 REAL, imb_025 REAL, buy_1s REAL, sell_1s REAL, cvd REAL, top_bid_wall REAL,
        top_bid_wall_px REAL, top_ask_wall REAL, top_ask_wall_px REAL, event_age REAL)""")
    for i in range(seconds):
        t = start + i
        for v in ("coinbase", "kraken"):
            db.execute("INSERT INTO book_seconds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (t, v, "LIVE" if live else "STALE", 60000 + i * 0.1, 0.1, 5, 4, 0.1, 0.2, 0.5, 0.25, i * 0.25,
                        3, 59990, 2, 60010, 0.1))
    db.commit()


def test_features_use_only_rows_at_or_before_the_decision(tmp_path):
    t0 = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc).timestamp()
    _flow_db(tmp_path / "f.db", t0, 400)
    fl = ex.Flow(tmp_path / "f.db")
    decision = datetime.fromtimestamp(t0 + 320.5, timezone.utc)
    fams, why = fl.families(decision)
    assert why is None
    assert all(datetime.fromisoformat(f["received_at"]).timestamp() <= decision.timestamp() for f in fams.values())
    assert abs(fams["price"]["values"]["return_60s"] - ((60000 + 320 * 0.1) / (60000 + 260 * 0.1) - 1)) < 1e-12
    assert fams["trades"]["values"]["kraken_buy_60s"] == pytest.approx(30.0)            # 60 rows x 0.5 BTC
    late = datetime.fromtimestamp(t0 + 420, timezone.utc)                              # newest row 21 s old: stale
    assert fl.families(late)[0] is None


def test_stale_books_are_excluded_not_dropped(tmp_path):
    t0 = datetime(2026, 11, 27, 4, 0, tzinfo=timezone.utc).timestamp()
    _flow_db(tmp_path / "f.db", t0, 400, live=False)
    fams, why = ex.Flow(tmp_path / "f.db").families(datetime.fromtimestamp(t0 + 320.5, timezone.utc))
    assert fams is None and "not LIVE" in why

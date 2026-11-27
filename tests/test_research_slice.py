"""The off-server research slice must give the loaders IDENTICAL rows. SYNTHETIC data."""
import copy
import json
import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import opening_research as research, research_slice
from src.signals_display.monitor import Journal
from tests.test_shadow_upgrade import _shift
from tests.test_signals_opening_models import opening, NOW


def build(path):
    journal = Journal(path)
    base, rng = opening(), random.Random(3)
    t0 = int(NOW.timestamp()) // 60 * 60 - 26 * 3600
    with journal.connect() as db:
        for i in range(26 * 60 + 400):  # contiguous 1-minute Coinbase candles + a non-Coinbase source
            start = t0 + 60 * i
            p = 100000 + 30 * math.sin(i / 37) + rng.gauss(0, 3)
            close = p + rng.gauss(0, 2)
            bar = {"start": start, "open": p, "high": max(p, close) + 1, "low": min(p, close) - 1, "close": close,
                   "volume": 1 + i % 7}
            received = datetime.fromtimestamp(start + 61, timezone.utc).isoformat()
            db.execute("INSERT INTO btc_candles VALUES (?,?,?,?)", ("Coinbase BTC-USD", start, json.dumps(bar), received))
            db.execute("INSERT INTO btc_candles VALUES (?,?,?,?)", ("Kraken XBT/USD", start, json.dumps(bar), received))
        for i in range(30):  # live frozen openings with quotes around the call
            row = _shift(copy.deepcopy(base), timedelta(minutes=15 * i))
            row["ticker"] = f"KXBTC15M-LIVE{i:03d}"
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", row["ticker"], row["generated_at"], row["data_cutoff"], row["closes_at"],
                        row["up"], row["status"], json.dumps(row)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (row["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(), i % 2))
            called = datetime.fromisoformat(row["generated_at"])
            for k in range(-5, 90, 7):
                at = (called + timedelta(seconds=k)).isoformat()
                if i % 3:  # some contracts have snapshots, others only predictions
                    db.execute("INSERT INTO opening_quote_snapshots (mode,ticker,observed_at,opens_at,seconds_from_open,"
                               "up_bid,up_ask,down_bid,down_ask,fee_multiplier,payload) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               ("PUBLIC MARKET DATA", row["ticker"], at, row["opens_at"], k, 0.48, 0.51, 0.48, 0.52, 1.0,
                                json.dumps({"big": "x" * 500})))
                payload = {"stale": k == 2, "junk": "y" * 800,
                           "market": {"quotes": {"up": {"bid": 0.47, "ask": 0.5 + k / 1000}, "down": {"ask": 0.53, "bid": 0.4}},
                                      "fee_multiplier": None if i % 4 == 0 else 1.0, "opens_at": row["opens_at"],
                                      "closes_at": row["closes_at"], "strike": 100000.0, "mapping_verified": True,
                                      "comparison_operator": ">=", "averaging_rule_verified": True, "other": [1, 2, 3]}}
                db.execute("INSERT INTO predictions (mode,ticker,timestamp,payload) VALUES (?,?,?,?)",
                           ("PUBLIC MARKET DATA", row["ticker"], at, json.dumps(payload)))
        for i in range(40):  # settled contracts WITHOUT frozen openings -> retrospective rows
            opened = datetime.fromtimestamp(t0 + 25 * 3600 + 900 * (i % 20), timezone.utc)
            ticker = f"KXBTC15M-RETRO{i:03d}"
            for k in range(3):
                payload = {"stale": False, "junk": "z" * 900, "market": {
                    "opens_at": opened.isoformat(), "closes_at": (opened + timedelta(minutes=15)).isoformat(),
                    "strike": 100000.0 + rng.gauss(0, 20), "mapping_verified": i % 9 != 0,
                    "comparison_operator": ">=", "averaging_rule_verified": True,
                    "quotes": {"up": {"bid": 0.4, "ask": 0.45}, "down": {"ask": 0.6}}, "fee_multiplier": 1.0}}
                db.execute("INSERT INTO predictions (mode,ticker,timestamp,payload) VALUES (?,?,?,?)",
                           ("PUBLIC MARKET DATA", ticker, (opened + timedelta(seconds=30 * k)).isoformat(), json.dumps(payload)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (ticker, "official", (opened + timedelta(minutes=16)).isoformat(), i % 2))


def loaded(path, asof):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        live = research.load(db, asof)
        retro, info = research.retrospective_rows(db, {r["ticker"] for r in live}, asof=asof)
    return live, retro, info


def test_slice_gives_identical_loader_output_and_is_much_smaller(tmp_path):
    full = tmp_path / "full.db"
    build(full)
    asof = NOW + timedelta(days=30)
    before = full.read_bytes()
    manifest = research_slice.export(full, tmp_path / "slice.db", asof, progress=lambda _m: None)
    assert full.read_bytes() == before                        # source untouched
    a, b = loaded(full, asof), loaded(tmp_path / "slice.db", asof)
    assert len(a[0]) == 30 and len(a[1]) > 10                  # the test exercises both loaders
    assert sum(r["quote"] is not None for r in a[0]) == 30
    assert {r["quote"]["source"] for r in a[0]} == {"predictions", "opening_quote_snapshots"}
    assert a == b
    with sqlite3.connect(tmp_path / "slice.db") as db:
        assert db.execute("SELECT MAX(LENGTH(payload)) FROM predictions").fetchone()[0] < 500   # junk fields dropped
        assert db.execute("SELECT COUNT(*) FROM btc_candles WHERE source NOT LIKE 'Coinbase%'").fetchone()[0] == 0
        assert db.execute("SELECT MAX(LENGTH(payload)) FROM opening_quote_snapshots").fetchone()[0] == 2
    assert manifest["counts"]["btc_engine_opening"] == 30
    assert json.loads((tmp_path / "slice.db.manifest.json").read_text())["sha256"] == manifest["sha256"]


def test_slice_refuses_to_overwrite(tmp_path):
    full = tmp_path / "full.db"
    Journal(full)
    (tmp_path / "slice.db").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        research_slice.export(full, tmp_path / "slice.db", NOW, progress=lambda _m: None)


def test_reassembly_verifies_parts_and_hash(tmp_path):
    import base64, hashlib, re, subprocess, tarfile
    (tmp_path / "slice.db").write_bytes(b"db-bytes" * 1000)
    with tarfile.open(tmp_path / "bundle.tgz", "w:gz") as tar:
        tar.add(tmp_path / "slice.db", arcname="slice.db")
    data = (tmp_path / "bundle.tgz").read_bytes()
    text, digest = base64.b64encode(data).decode(), hashlib.sha256(data).hexdigest()
    half = len(text) // 2
    parts = []
    for i, chunk in enumerate((text[:half], text[half:]), 1):
        p = tmp_path / f"slice.part-0{i}.txt"
        p.write_text(f"KXSLICE {i} 2 {digest}\n{chunk}")
        parts.append(p)
    info = research_slice.reassemble(list(reversed(parts)), tmp_path / "out")      # order-independent
    assert info["parts"] == 2 and (tmp_path / "out" / "slice.db").read_bytes() == b"db-bytes" * 1000
    with pytest.raises(ValueError):
        research_slice.reassemble(parts[:1], tmp_path / "out2")                    # missing part
    parts[1].write_text(f"KXSLICE 2 2 {digest}\nAAAA")
    with pytest.raises(ValueError):
        research_slice.reassemble(parts, tmp_path / "out3")                        # corrupted


def test_export_script_never_touches_live_services():
    import re, subprocess
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "deploy/oracle/export_slice_8768.sh"
    code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    assert "8765" not in code and "8767" not in code and "systemctl" not in code
    assert re.findall(r"kill -TERM \"\$p\"", code) and "src.signals_display." in code   # only our research runs
    assert all('rm -rf "$OUT' in l or 'rm -f "$c"' in l or 'rm -f "$OUT/bundle.tgz"' in l
               for l in code.splitlines() if re.search(r"\brm\b", l))
    assert "research_slice" in code and "--asof" in code

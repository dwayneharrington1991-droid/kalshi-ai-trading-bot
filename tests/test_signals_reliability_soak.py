"""Fast deterministic reliability soak; all inputs are synthetic and offline."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

from src.signals_display.monitor import Journal, analyze
from tests.test_signals_display import NOW, snapshot


def test_repeated_rollovers_keep_learning_rows_bounded_and_journal_responsive(tmp_path):
    journal = Journal(tmp_path / "soak.db")
    for index in range(32):
        opened = NOW + timedelta(minutes=15 * index)
        data = deepcopy(snapshot())
        data.update(clock=opened.isoformat(), now=opened.isoformat(), mode="REPLAY / SIMULATED")
        market = data["markets"][0]
        market.update(
            ticker=f"KXBTC15M-SOAK{index:02d}", opens_at=opened.isoformat(),
            closes_at=(opened + timedelta(minutes=15)).isoformat(), observed_at=opened.isoformat(),
        )
        data["reference"] = {
            "price": 100020, "observed_at": opened.isoformat(),
            "source": "synthetic soak reference", "official": True,
        }
        state = analyze(data, opened)
        state["model"] = {
            "version": "soak-v1",
            "candidate_features": {"values": {"return_1m": index / 10000}},
            "coverage": {"synthetic": {"status": "LIVE", "debug": "z" * 500_000}},
        }
        journal.step(state)
    with journal.read() as db:
        rows = db.execute("SELECT payload FROM predictions ORDER BY id").fetchall()
        assert len(rows) == 32
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert all(len(row[0]) < 65536 for row in rows)
    assert json.loads(rows[-1][0])["market"]["ticker"] == "KXBTC15M-SOAK31"
    assert Path(journal.path).stat().st_size < 2_000_000

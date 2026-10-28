"""Dense evidence is bounded and mistakes are learned only after official outcomes."""
import json
from datetime import timedelta

from src.signals_display.monitor import Journal, analyze
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame


def test_dense_learning_records_once_per_second_and_rollover_immediately(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    for offset in (0, 0.25, 0.75, 1.0, 1.25):
        state = analyze(frame(offset), NOW + timedelta(seconds=offset))
        journal.step(state)
    with journal.read() as db:
        assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2
    next_state = analyze(frame(2), NOW + timedelta(seconds=2))
    next_state["market"]["ticker"] = "KXBTC15M-NEXT"
    journal.step(next_state)
    with journal.read() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM predictions WHERE ticker='KXBTC15M-NEXT'"
        ).fetchone()[0] == 1


def test_official_settlement_writes_one_immutable_mistake_review(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    state = analyze(frame(), NOW)
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL",
        "ticker": state["market"]["ticker"],
        "updated_at": state["now"],
        "closes_at": state["market"]["closes_at"],
        "data_cutoff": state["now"],
        "up": 0.8,
        "regime": "test",
        "version": "test-v1",
        "threshold": state["market"]["strike"],
        "initial": {},
    }
    journal.step(state)
    ticker = state["market"]["ticker"]
    assert journal.settle(ticker, "no", state["mode"], (NOW + timedelta(minutes=20)).isoformat())
    assert not journal.settle(ticker, "no", state["mode"], (NOW + timedelta(minutes=21)).isoformat())
    with journal.read() as db:
        rows = db.execute("SELECT payload FROM learning_contract_reviews").fetchall()
    assert len(rows) == 1
    review = json.loads(rows[0][0])
    assert review["official_outcome"] == "DOWN"
    assert review["opening"]["correct"] is False
    assert review["opening"]["brier"] > 0
    assert review["recorded_forecasts"] == 1

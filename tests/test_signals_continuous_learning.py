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


def test_compact_minute_memory_preserves_first_and_latest_second(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    first = analyze(frame(), NOW)
    first["prediction_engine"] = {
        "up": 0.51, "version": "shadow-v1", "data_cutoff": first["now"]
    }
    first["model"]["candidate_features"] = {"values": {"momentum_1m": 0.01}}
    journal.step(first)

    latest = analyze(frame(2), NOW + timedelta(seconds=2))
    latest["prediction_engine"] = {
        "up": 0.54, "version": "shadow-v1", "data_cutoff": latest["now"]
    }
    latest["model"]["candidate_features"] = {"values": {"momentum_1m": 0.03}}
    journal.step(latest)

    with journal.read() as db:
        row = db.execute(
            "SELECT samples,first_payload,last_payload FROM intelligence_minute_memory"
        ).fetchone()
        assert db.execute("SELECT COUNT(*) FROM forecast_outcomes").fetchone()[0] == 0
    assert row[0] == 2
    assert json.loads(row[1])["probability_up"] == 0.51
    assert json.loads(row[1])["candidate_features"]["momentum_1m"] == 0.01
    assert json.loads(row[2])["probability_up"] == 0.54
    assert json.loads(row[2])["candidate_features"]["momentum_1m"] == 0.03

    readiness = journal.learning_readiness()["intelligence_memory"]
    assert readiness["minute_records"] == 1
    assert readiness["second_level_samples"] == 2
    assert "official outcomes" in readiness["policy"]


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


def test_bounded_backfill_reviews_old_forecasts_without_creating_training_inputs(tmp_path):
    journal = Journal(tmp_path / "journal.db")
    state = analyze(frame(), NOW)
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL", "ticker": state["market"]["ticker"],
        "updated_at": state["now"], "closes_at": state["market"]["closes_at"],
        "data_cutoff": state["now"], "up": 0.7, "regime": "test",
        "version": "old-v1", "threshold": state["market"]["strike"], "initial": {},
    }
    journal.step(state)
    ticker = state["market"]["ticker"]
    with journal.connect() as db:
        db.execute(
            "UPDATE btc_engine_predictions SET mode='PUBLIC MARKET DATA' WHERE ticker=?",
            (ticker,),
        )
        db.execute(
            "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
            (ticker, "official", (NOW + timedelta(minutes=20)).isoformat(), 0),
        )
        before_predictions = db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        before_openings = db.execute("SELECT COUNT(*) FROM opening_input_snapshots").fetchone()[0]

    assert journal.backfill_contract_reviews(limit=20) == 1
    assert journal.backfill_contract_reviews(limit=20) == 0
    with journal.read() as db:
        assert db.execute("SELECT COUNT(*) FROM learning_contract_reviews").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == before_predictions
        assert db.execute("SELECT COUNT(*) FROM opening_input_snapshots").fetchone()[0] == before_openings

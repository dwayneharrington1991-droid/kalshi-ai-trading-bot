import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import opening_challengers as oc
from src.signals_display import opening_shadow as sh

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
NAMES = ["x", "z"]


def _fitted():
    train = []
    for i in range(160):
        x = (i % 9) - 4.0
        train.append({"features": {"x": x, "z": float(i % 5)}, "outcome": 1 if x > 0 else 0,
                      "timestamp": (T0 + timedelta(minutes=15 * i)).isoformat(),
                      "label_available_at": (T0 + timedelta(minutes=15 * i + 15)).isoformat()})
    return oc.fit_logistic(train, NAMES)


def test_decision_band_is_the_fixed_055_rule():
    assert oc.call_of(0.55) == "UP" and oc.call_of(0.45) == "DOWN" and oc.call_of(0.5) == "WAIT"


def test_ledger_keeps_one_row_per_ticker_and_refuses_edits(tmp_path):
    db = tmp_path / "led.sqlite3"
    row = {"ticker": "KX-1", "opens_at": T0.isoformat(), "scored_at": T0.isoformat(), "seconds_after_open": 1.0,
           "inputs_complete": 1, "probability": 0.6, "decision": "UP", "model_sha256": "m"}
    assert sh.write_ledger(db, [row]) == 1
    assert sh.write_ledger(db, [{**row, "probability": 0.1, "decision": "DOWN"}]) == 0   # duplicate ignored
    with sqlite3.connect(db) as led:
        assert led.execute("SELECT COUNT(*), MAX(decision) FROM shadow_openings").fetchone() == (1, "UP")
        with pytest.raises(sqlite3.DatabaseError):
            led.execute("UPDATE shadow_openings SET decision='DOWN'")
        with pytest.raises(sqlite3.DatabaseError):
            led.execute("DELETE FROM shadow_openings")


def test_incomplete_inputs_are_recorded_as_not_scored_never_guessed():
    fitted = _fitted()
    rows = sh.score_openings([("KX-2", T0.isoformat(), T0.isoformat(), {"x": 1.0})], fitted, "m")
    assert rows[0]["inputs_complete"] == 0 and rows[0]["decision"] == "NOT_SCORED" and rows[0]["probability"] is None


def test_complete_inputs_get_a_probability_and_a_band_decision():
    fitted = _fitted()
    rows = sh.score_openings([("KX-3", T0.isoformat(), T0.isoformat(), {"x": 3.0, "z": 1.0})], fitted, "m")
    assert rows[0]["inputs_complete"] == 1
    assert rows[0]["decision"] == oc.call_of(rows[0]["probability"])


def test_saved_model_reproduces_the_same_probabilities(tmp_path):
    fitted = _fitted()
    payload = sh.serialize_model(fitted, T0, 160)
    loaded = sh.load_model(payload)
    probe = {"x": 2.0, "z": 3.0}
    assert oc.predict_logistic(loaded, [sh.scoring_row(probe)], calibrated=True) == \
        oc.predict_logistic(fitted, [sh.scoring_row(probe)], calibrated=True)
    assert payload["sha256"] and payload["training_rows"] == 160


def test_model_is_refit_at_most_once_a_day():
    fitted = _fitted()
    payload = sh.serialize_model(fitted, T0, 160)
    assert sh.model_is_fresh(payload, T0 + timedelta(hours=23))
    assert not sh.model_is_fresh(payload, T0 + timedelta(hours=25))
    assert not sh.model_is_fresh(None, T0)


def test_the_journal_connection_used_for_scoring_cannot_write(tmp_path):
    journal = tmp_path / "j.db"
    setup = sqlite3.connect(journal)
    setup.execute("CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT)")
    setup.commit()
    setup.close()
    ro = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA','x')")
    finally:
        ro.close()


def test_only_new_recorded_openings_are_selected_and_scored_once(tmp_path):
    import json
    journal = tmp_path / "j.db"
    setup = sqlite3.connect(journal)
    setup.execute("CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT, generated_at TEXT, cutoff TEXT, "
                  "closes_at TEXT, up REAL, status TEXT, payload TEXT)")
    def add(ticker, generated):
        payload = json.dumps({"opens_at": generated, "frozen_features": {"values": {"x": 1.0, "z": 2.0}}})
        setup.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,?,?)",
                      (ticker, generated, generated, generated, 0.5, "ok", payload))
    add("KX-OLD", (T0 - timedelta(minutes=15)).isoformat())
    add("KX-NEW", T0.isoformat())
    add("KX-FUTURE", (T0 + timedelta(minutes=15)).isoformat())
    setup.commit()
    setup.close()
    ledger = tmp_path / "shadow_ledger.sqlite3"
    sh.write_ledger(ledger, [{"ticker": "KX-OLD", "opens_at": "a", "scored_at": "a", "seconds_after_open": 0.0,
                              "inputs_complete": 1, "probability": 0.5, "decision": "WAIT", "model_sha256": "m"}])
    ro = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True)
    try:
        selected = sh.unscored_openings(ro, ledger, NAMES, T0 + timedelta(minutes=1))
    finally:
        ro.close()
    assert [s[0] for s in selected] == ["KX-NEW"]

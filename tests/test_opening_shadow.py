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


def _version(trained_at, cutoff=None):
    return sh.serialize_model(_fitted(), trained_at, 160, cutoff)


def test_decision_band_is_the_fixed_055_rule():
    assert oc.call_of(0.55) == "UP" and oc.call_of(0.45) == "DOWN" and oc.call_of(0.5) == "WAIT"


def test_ledger_keeps_one_row_per_ticker_and_refuses_edits(tmp_path):
    db = tmp_path / "led.sqlite3"
    row = {"ticker": "KX-1", "opens_at": T0.isoformat(), "generated_at": T0.isoformat(), "scored_at": T0.isoformat(),
           "seconds_after_open": 1.0, "inputs_complete": 1, "probability": 0.6, "decision": "UP", "model_sha256": "m"}
    assert sh.write_ledger(db, [row]) == 1
    assert sh.write_ledger(db, [{**row, "probability": 0.1, "decision": "DOWN"}]) == 0
    with sqlite3.connect(db) as led:
        assert led.execute("SELECT COUNT(*), MAX(decision) FROM shadow_openings").fetchone() == (1, "UP")
        with pytest.raises(sqlite3.DatabaseError):
            led.execute("UPDATE shadow_openings SET decision='DOWN'")
        with pytest.raises(sqlite3.DatabaseError):
            led.execute("DELETE FROM shadow_openings")


def test_incomplete_inputs_are_recorded_as_not_scored_never_guessed():
    v = _version(T0 - timedelta(days=1))
    rows = sh.score_openings([("KX-2", T0.isoformat(), T0.isoformat(), {"x": 1.0})], [v], T0 + timedelta(seconds=5))
    assert rows[0]["inputs_complete"] == 0 and rows[0]["decision"] == "NOT_SCORED" and rows[0]["probability"] is None


def test_complete_inputs_get_a_probability_and_a_band_decision():
    v = _version(T0 - timedelta(days=1))
    rows = sh.score_openings([("KX-3", T0.isoformat(), T0.isoformat(), {"x": 3.0, "z": 1.0})], [v], T0)
    assert rows[0]["inputs_complete"] == 1 and rows[0]["decision"] == oc.call_of(rows[0]["probability"])
    assert rows[0]["model_sha256"] == v["sha256"]


def test_an_opening_generated_before_any_model_is_never_scored_with_a_later_model():
    later = _version(T0 + timedelta(hours=2))
    rows = sh.score_openings([("KX-4", T0.isoformat(), T0.isoformat(), {"x": 3.0, "z": 1.0})], [later], T0)
    assert rows[0]["decision"] == "NOT_SCORED" and rows[0]["model_sha256"] == "NO_MODEL_BEFORE_OPENING"


def test_each_opening_is_scored_with_the_newest_model_trained_before_it():
    old = _version(T0 - timedelta(days=2))
    new = _version(T0 - timedelta(hours=1))
    assert sh.version_for([old, new], T0.isoformat())["sha256"] == new["sha256"]
    assert sh.version_for([old, new], (T0 - timedelta(days=1)).isoformat())["sha256"] == old["sha256"]


def test_versions_are_write_once_and_keep_their_training_cutoff(tmp_path):
    v = _version(T0, cutoff=(T0 - timedelta(minutes=5)).isoformat())
    path = sh.write_version(tmp_path, v)
    assert path.exists() and sh.load_versions(tmp_path)[0]["training_cutoff"] == v["training_cutoff"]
    with pytest.raises(FileExistsError):
        sh.write_version(tmp_path, v)


def test_genesis_is_written_once(tmp_path):
    first = sh.genesis(tmp_path, T0)
    later = sh.genesis(tmp_path, T0 + timedelta(days=3))
    assert first == later == T0


def test_only_openings_after_genesis_without_outcome_are_selected(tmp_path):
    import json
    journal = tmp_path / "j.db"
    db = sqlite3.connect(journal)
    db.execute("CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT, generated_at TEXT, cutoff TEXT, "
               "closes_at TEXT, up REAL, status TEXT, payload TEXT)")
    db.execute("CREATE TABLE forecast_outcomes (ticker TEXT, origin TEXT, observed_at TEXT, outcome INTEGER)")
    def add(ticker, generated):
        payload = json.dumps({"opens_at": generated, "frozen_features": {"values": {"x": 1.0, "z": 2.0}}})
        db.execute("INSERT INTO btc_engine_opening VALUES ('PUBLIC MARKET DATA',?,?,?,?,?,?,?)",
                   (ticker, generated, generated, generated, 0.5, "ok", payload))
    add("KX-BEFORE-GENESIS", (T0 - timedelta(minutes=15)).isoformat())
    add("KX-NEW", (T0 + timedelta(minutes=1)).isoformat())
    add("KX-SETTLED", (T0 + timedelta(minutes=2)).isoformat())
    db.execute("INSERT INTO forecast_outcomes VALUES ('KX-SETTLED','official',?,1)",
               ((T0 + timedelta(minutes=17)).isoformat(),))
    db.commit()
    db.close()
    ro = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True)
    try:
        selected = sh.unscored_openings(ro, tmp_path, NAMES, T0, T0 + timedelta(minutes=5))
    finally:
        ro.close()
    assert [s[0] for s in selected] == ["KX-NEW"]


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


def test_outcome_copy_takes_only_official_outcomes_for_shadow_tickers_and_is_idempotent(tmp_path):
    journal = tmp_path / "j.db"
    db = sqlite3.connect(journal)
    db.execute("CREATE TABLE forecast_outcomes (ticker TEXT, origin TEXT, observed_at TEXT, outcome INTEGER, "
               "PRIMARY KEY(ticker, origin))")
    db.execute("INSERT INTO forecast_outcomes VALUES ('KX-A','official',?,1)", ((T0 + timedelta(minutes=16)).isoformat(),))
    db.execute("INSERT INTO forecast_outcomes VALUES ('KX-B','official',?,0)", ((T0 + timedelta(minutes=17)).isoformat(),))
    db.execute("INSERT INTO forecast_outcomes VALUES ('KX-NOT-SHADOW','official',?,1)", ((T0 + timedelta(minutes=16)).isoformat(),))
    db.execute("INSERT INTO forecast_outcomes VALUES ('KX-A','predicted',?,0)", ((T0 + timedelta(minutes=16)).isoformat(),))
    db.commit()
    db.close()
    ledger = tmp_path / "led.sqlite3"
    sh.write_ledger(ledger, [
        {"ticker": "KX-A", "opens_at": T0.isoformat(), "generated_at": T0.isoformat(), "scored_at": T0.isoformat(),
         "seconds_after_open": 1.0, "inputs_complete": 1, "probability": 0.6, "decision": "UP", "model_sha256": "m"},
        {"ticker": "KX-B", "opens_at": T0.isoformat(), "generated_at": T0.isoformat(), "scored_at": T0.isoformat(),
         "seconds_after_open": 1.0, "inputs_complete": 1, "probability": 0.4, "decision": "DOWN", "model_sha256": "m"},
    ])
    ro = sqlite3.connect(f"file:{journal.resolve()}?mode=ro", uri=True)
    try:
        assert sh.sync_outcomes(ro, ledger, T0) == 2
        assert sh.sync_outcomes(ro, ledger, T0) == 0          # idempotent: nothing new on a second run
    finally:
        ro.close()
    with sqlite3.connect(ledger) as led:
        copied = dict(led.execute("SELECT ticker, outcome FROM shadow_outcomes"))
        assert copied == {"KX-A": 1, "KX-B": 0}              # the non-shadow ticker and the 'predicted' origin are absent
        with pytest.raises(sqlite3.DatabaseError):
            led.execute("UPDATE shadow_outcomes SET outcome=0")

"""Phase 4 adversarial checks for live-call and artifact claims."""
import json
import sqlite3
from datetime import timedelta

from src.signals_display import artifact_diagnostics as diagnostics
from src.signals_display import opening_research, settled_audit
from tests.test_signals_research_resume import trained as trained_fixture
from tests.test_signals_settled_audit import ASOF, START, _db, _opening, _ro


def test_phase4_research_family_stays_within_preregistered_six():
    assert len(opening_research.MODELS) <= 6
    assert "regime_aware_compact" not in opening_research.MODELS
    assert "compact_plus_preopen_market" not in opening_research.MODELS


def test_conflicting_official_outcomes_are_quarantined(tmp_path):
    path = tmp_path / "conflict.db"
    opened = START
    closed = opened + timedelta(minutes=15)
    payload = json.dumps({
        "opens_at": opened.isoformat(), "data_cutoff": opened.isoformat(),
        "direction": "UP", "confidence": {"status": "CALIBRATED"},
        "data_quality": {"closed_history_bars": 1440},
        "frozen_features": {"version": 9, "max_input_received_at": opened.isoformat()},
    })
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE btc_engine_opening (mode,ticker,generated_at,cutoff,closes_at,up,payload)")
        db.execute("CREATE TABLE forecast_outcomes (ticker,origin,observed_at,outcome)")
        db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?)", (
            "PUBLIC MARKET DATA", "T", opened.isoformat(), opened.isoformat(),
            closed.isoformat(), 0.7, payload,
        ))
        db.executemany("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", [
            ("T", "official", (closed + timedelta(seconds=1)).isoformat(), 1),
            ("T", "official", (closed + timedelta(seconds=2)).isoformat(), 0),
        ])
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows, problems = settled_audit.load_openings(db, ASOF)
        integrity = settled_audit.integrity_checks(db, rows, ASOF)
    assert rows[0]["outcome"] is None
    assert problems == {"CONFLICTING_OR_INVALID_OFFICIAL_OUTCOME": 1}
    assert integrity["duplicate_official_outcome_rows"] == 1
    assert integrity["conflicting_or_invalid_official_outcome_tickers"] == 1


def test_threshold_challenge_keeps_alternatives_out_of_sealed_holdout(tmp_path):
    def build(db):
        for i in range(100):
            outcome = i % 2
            probability = 0.70 if outcome else 0.30
            _opening(db, i, probability, "UP" if outcome else "DOWN", outcome=outcome)

    with _ro(_db(tmp_path, build)) as db:
        report = settled_audit.audit(db, ASOF)["phase4_threshold_challenge"]
    assert set(report["development_alternatives"]) == {
        f"{x:.2f}" for x in settled_audit.PHASE4_THRESHOLD_CHALLENGERS
    }
    assert report["fixed_policy_on_sealed_holdout"]["threshold"] == 0.55
    assert report["fixed_policy_on_sealed_holdout"]["calls"] == 20
    assert report["selection_allowed"] is False


def test_artifact_reproduction_requires_matching_model_identity(trained_fixture):
    artifact, source = trained_fixture
    rows = [dict(row, features=dict(row["features"])) for row in source]
    for row in rows:
        row["recorded_up"] = diagnostics._predict(artifact["model"], row)
        row["recorded_model"] = "some-other-artifact"
    other = diagnostics.diagnose(artifact, rows)["reproduction"]
    assert other["status"] == "NO_MATCHING_MODEL_COHORT"
    assert other["different_model_id_openings"] == 40

    for row in rows:
        row["recorded_model"] = artifact["model_id"]
    matching = diagnostics.diagnose(artifact, rows)["reproduction"]
    assert matching["status"] == "REPRODUCED"
    assert matching["matching_model_id_exactly_reproduced"] == 40

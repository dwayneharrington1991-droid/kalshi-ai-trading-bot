"""Integrated Phase 4 (Claude + Codex): cross-cutting safeguards."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.signals_display import opening_research as research
from src.signals_display import artifact_diagnostics as diagnostics
from src.signals_display import settled_audit as audit
from tests.test_signals_settled_audit import ASOF, _db, _opening, _ro

ROOT = Path(__file__).resolve().parents[1]


def _conflicting_journal(tmp_path):
    def build(db):
        for i in range(30):
            _opening(db, i, 0.7, "UP", outcome=1, mid=0.5)
        # A conflicting second official row for one displayed call (only
        # possible in a journal without the (ticker, origin) primary key).
        db.execute("CREATE TABLE fo2 AS SELECT * FROM forecast_outcomes")
        db.execute("DROP TABLE forecast_outcomes")
        db.execute("ALTER TABLE fo2 RENAME TO forecast_outcomes")
        db.execute("INSERT INTO forecast_outcomes SELECT ticker,origin,observed_at,0 FROM forecast_outcomes "
                   "WHERE ticker='KXBTC15M-0005'")
    return _db(tmp_path, build)


def test_quarantined_settlement_is_excluded_from_scores_baselines_and_replica(tmp_path):
    with _ro(_conflicting_journal(tmp_path)) as db:
        outcomes, quarantined = audit.official_outcomes(db)
        history = audit.official_history(db, ASOF)
        report = audit.audit(db, ASOF)
    assert len(history) == 29 and {y for _t, y in history} == {1}  # the 0/1 conflict never enters
    assert quarantined == {"KXBTC15M-0005"} and "KXBTC15M-0005" not in outcomes
    assert report["journal_audit"]["verified_settled_openings"] == 29
    board = report["dashboard_scoreboard_replica"]
    assert board["calls"] == 29 and board["displayed_calls_excluded_for_conflicting_settlement"] == 1
    assert report["integrity_checks"]["conflicting_or_invalid_official_outcome_tickers"] == 1
    # Baselines read the same quarantined source: every remaining label is UP.
    base = report["all_settled"]["versus_market"]
    assert base["scores"]["expanding_base_rate"]["accuracy"] == 1.0


def test_earliest_receipt_is_kept_for_identical_repeated_settlements(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1)
        db.execute("CREATE TABLE fo2 AS SELECT * FROM forecast_outcomes")
        db.execute("DROP TABLE forecast_outcomes")
        db.execute("ALTER TABLE fo2 RENAME TO forecast_outcomes")
        # Same label, received earlier and written in a different ISO format.
        db.execute("INSERT INTO forecast_outcomes VALUES ('KXBTC15M-0000','official','2026-10-01T00:15:30Z',1)")
    with _ro(_db(tmp_path, build)) as db:
        outcomes, quarantined = audit.official_outcomes(db)
    assert not quarantined
    assert outcomes["KXBTC15M-0000"][0] == "2026-10-01T00:15:30Z"  # earlier than 00:16:00+00:00


def test_invalid_official_row_quarantines_ticker_instead_of_being_ignored(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1)
        db.execute("CREATE TABLE fo2 AS SELECT * FROM forecast_outcomes")
        db.execute("DROP TABLE forecast_outcomes")
        db.execute("ALTER TABLE fo2 RENAME TO forecast_outcomes")
        db.execute("INSERT INTO forecast_outcomes VALUES ('KXBTC15M-0000','official',?,2)",
                   (ASOF.isoformat(),))
    with _ro(_db(tmp_path, build)) as db:
        outcomes, quarantined = audit.official_outcomes(db)
        report = audit.audit(db, ASOF)
    assert not outcomes and quarantined == {"KXBTC15M-0000"}
    assert report["integrity_checks"]["invalid_official_outcome_rows"] == 1
    assert report["journal_audit"]["verified_settled_openings"] == 0


def test_threshold_challenge_never_pools_model_versions_silently(tmp_path):
    def build(db):
        for i in range(60):
            _opening(db, i, 0.7, "UP", outcome=i % 2)
        db.execute("UPDATE btc_engine_opening SET payload=json_set(payload,'$.model','m2') "
                   "WHERE CAST(substr(ticker,10) AS INTEGER)>=30")
    with _ro(_db(tmp_path, build)) as db:
        challenge = audit.audit(db, ASOF)["phase4_threshold_challenge"]
    assert challenge["model_scope"] == "MIXED_MODEL_VERSIONS_REFERENCE_ONLY"
    assert set(challenge["by_model"]) == {"m1", "m2"}
    for sub in challenge["by_model"].values():
        assert sub["selection_allowed"] is False and sub["fixed_policy"] == 0.55


def test_report_b_drops_contracts_joined_to_more_than_one_official_row(monkeypatch):
    rows = [{"ticker": "A", "features": {}, "timestamp": "2026-10-01T00:00:00+00:00",
             "opens_at": "2026-10-01T00:00:00+00:00", "outcome": 1},
            {"ticker": "A", "features": {}, "timestamp": "2026-10-01T00:00:00+00:00",
             "opens_at": "2026-10-01T00:00:00+00:00", "outcome": 0},
            {"ticker": "B", "features": {}, "timestamp": "2026-10-01T00:15:00+00:00",
             "opens_at": "2026-10-01T00:15:00+00:00", "outcome": 1}]
    monkeypatch.setattr(research.opening_models, "dataset", lambda db, asof: [dict(r) for r in rows])
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE btc_engine_opening (mode,ticker,up,payload)")
    assert [r["ticker"] for r in research.load(db, ASOF)] == ["B"]


def test_candidate_family_is_six_and_includes_momentum_trend_volatility():
    assert len(research.MODELS) == research.PREREGISTERED_CANDIDATE_LIMIT == 6
    features = research.MODELS["momentum_trend_volatility"]["features"]
    assert set(research.TREND) <= set(features)
    assert set(research.INTERACTIONS) <= set(features)


def test_artifact_report_counts_four_components_and_exact_87_input_union(monkeypatch):
    names = [f"feature_{i}" for i in range(87)]
    components = [
        {"kind": kind, "feature_set": f"set-{i}",
         "preprocessor": {"names": names[i::4]}}
        for i, kind in enumerate(("logistic", "ridge", "opening_gradient_boosting", "volatility_baseline"))
    ]
    artifact = {"model_id": "m87", "model": {"kind": "opening_ensemble",
                "components": components, "mixture_weights": [0.25] * 4},
                "available_at": "2026-01-01T00:00:00+00:00",
                "labels_available_through": "2025-12-31T00:00:00+00:00"}
    row = {"ticker": "T", "features": {name: 1.0 for name in names}, "baseline": 0.5,
           "recorded_up": 0.6, "recorded_model": "opening-candidate/m87", "outcome": 1,
           "opens_at": "2026-01-01T00:15:00+00:00"}
    monkeypatch.setattr(diagnostics, "_predict", lambda model, row, features=None: 0.6)
    report = diagnostics.diagnose(artifact, [row])
    assert report["component_count"] == report["active_components"] == 4
    assert report["input_feature_count"] == 87
    assert report["requested_87_inputs_matches_artifact"] is True


def test_runner_pins_report_b_and_artifact_diagnostics_to_port_8768_journal():
    script = (ROOT / "deploy/oracle/research_readonly.sh").read_text()
    assert 'preview_journal=$(journal_of "$pid")' in script
    assert "journal=$preview_journal" in script
    assert "largest historical journal (Report A only if different)" in script
    assert 'systemctl' not in script and 'kill ' not in script


def test_research_tools_cannot_overwrite_or_activate_the_model(tmp_path):
    from tests.test_signals_research_resume import _rows  # noqa: F401  (fixture module import)
    artifact = tmp_path / "opening.json"
    artifact.write_text(json.dumps({"model_id": "x", "model": {"kind": "logistic"}, "format": "artifact"}))
    before = hashlib.sha256(artifact.read_bytes()).hexdigest()
    journal = _db(tmp_path, lambda db: _opening(db, 0, 0.7, "UP", outcome=1))
    for module, extra in (("artifact_diagnostics", ["--artifact", str(artifact)]), ("opening_research", [])):
        result = subprocess.run(
            [sys.executable, "-m", f"src.signals_display.{module}", "--journal", str(journal),
             "--output", str(artifact), *extra],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert result.returncode != 0 and "Refusing to overwrite" in result.stderr, module
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before
    report = research.research([])
    assert report["activation_allowed"] is False and report["promotion_allowed"] is False


def test_partially_covered_candidate_family_is_labelled_not_silently_relabelled():
    from src.signals_display import rigorous_synthetic as synthetic
    from tests.test_signals_opening_research import _augment

    rows = _augment(synthetic.opening_rows(300, seed=606, signal=0.4))
    for r in rows:  # trend inputs absent; regime indicators always present
        for name in research.TREND:
            r["features"].pop(name, None)
    report = research.research(rows)
    coverage = report["candidate_incremental_coverage_development"]["momentum_trend_volatility"]
    assert coverage["status"] == "PARTIAL_FAMILY"
    assert set(research.TREND) <= set(coverage["sparse_incremental_features"])
    assert "momentum_trend_volatility" not in report["inactive_research_candidates"]

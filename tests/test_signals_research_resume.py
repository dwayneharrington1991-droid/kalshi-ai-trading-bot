"""Report B speed, resumable checkpoints, Report A diagnostics and model diagnostics."""
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.signals_display import artifact_diagnostics as diag
from src.signals_display import learning, opening_models
from src.signals_display import opening_research as research
from src.signals_display import research_fast as fast
from src.signals_display import rigorous_evaluation as rigor
from src.signals_display import rigorous_synthetic as synthetic
from src.signals_display import settled_audit as audit
from tests.test_signals_opening_research import _augment, _retro_journal
from tests.test_signals_settled_audit import ASOF, _db, _opening, _ro


def _rows(n, *, missing, separation, seed):
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        base = rng.gauss(0, 1)
        features = {}
        for j in range(12):
            value = 0.9 * base + 0.1 * rng.gauss(0, 1) if j < 3 else rng.gauss(0, 1)
            if rng.random() > missing:
                features[f"f{j}"] = value
        rows.append({"features": features, "baseline": min(0.95, max(0.05, rng.random())),
                     "outcome": int(rng.random() < learning.sigmoid(separation * base))})
    return rows


@pytest.mark.parametrize("missing,separation,l2,offset", [
    (0.0, 0.5, 3.0, True), (0.4, 0.5, 3.0, True), (0.3, 6.0, 3.0, False), (0.2, 1.0, 300.0, True),
])
def test_vectorized_solver_matches_the_original_algorithm(missing, separation, l2, offset):
    rows = _rows(500, missing=missing, separation=separation, seed=int(separation * 100 + missing * 10))
    prep = research._preprocess(rows, [f"f{j}" for j in range(12)])
    original = rigor.fit_logistic(rows, prep, l2, use_offset=offset)
    vectorized = fast.fit_logistic(rows, prep, l2, use_offset=offset)
    assert vectorized["newton_iterations"] == original["newton_iterations"]
    assert vectorized["converged"] == original["converged"]
    assert vectorized["weights"] == pytest.approx(original["weights"], abs=1e-9)
    for row in rows:
        assert rigor.raw_probability(vectorized, row) == pytest.approx(rigor.raw_probability(original, row), abs=1e-12)


def test_solver_falls_back_to_original_without_numpy(monkeypatch):
    rows = _rows(120, missing=0.1, separation=1.0, seed=3)
    prep = research._preprocess(rows, [f"f{j}" for j in range(12)])
    monkeypatch.setattr(fast, "np", None)
    assert fast.fit_logistic(rows, prep, 3.0, use_offset=True) == rigor.fit_logistic(rows, prep, 3.0, use_offset=True)


class Interrupted(Exception):
    """Stands in for the time limit killing the run mid-fold."""


_ORIGINAL_FIT = research._fit


def _counting_fit(monkeypatch, fail_after=None):
    calls = {"n": 0}
    original = _ORIGINAL_FIT

    def counted(train, validation, spec):
        calls["n"] += 1
        if fail_after is not None and calls["n"] > fail_after:
            raise Interrupted
        return original(train, validation, spec)

    monkeypatch.setattr(research, "_fit", counted)
    return calls


def _research_rows():
    rows = research.opening_models._validate_rows(_augment(synthetic.opening_rows(330, seed=411, signal=0.5)))
    return rows, len(rows) - int(round(len(rows) * research.SEALED_HOLDOUT_FRACTION))


def test_resumed_walk_forward_reuses_every_completed_fold_and_matches_exactly(tmp_path, monkeypatch):
    rows, seal = _research_rows()
    fresh, folds = research.walk_forward(rows, seal=seal)
    store = fast.Checkpoints(tmp_path / "ck")
    calls = _counting_fit(monkeypatch, fail_after=11)
    with pytest.raises(Interrupted):
        research.walk_forward(rows, seal=seal, checkpoint=store)
    assert calls["n"] == 12  # 11 results saved before the interruption
    calls = _counting_fit(monkeypatch)
    store = fast.Checkpoints(tmp_path / "ck")
    resumed, resumed_folds = research.walk_forward(rows, seal=seal, checkpoint=store)
    assert store.hits == 11 and calls["n"] == folds * len(research.MODELS) - 11
    assert resumed_folds == folds and resumed == fresh  # identical predictions
    calls = _counting_fit(monkeypatch)
    again, _ = research.walk_forward(rows, seal=seal, checkpoint=fast.Checkpoints(tmp_path / "ck"))
    assert calls["n"] == 0 and again == fresh


def test_changed_training_input_invalidates_only_affected_folds(tmp_path, monkeypatch):
    rows, seal = _research_rows()
    research.walk_forward(rows, seal=seal, checkpoint=fast.Checkpoints(tmp_path / "ck"))
    changed = [dict(r, features=dict(r["features"])) for r in rows]
    late = seal - 5  # only in the last development folds and the sealed fit
    changed[late]["features"]["return_5m"] = 123.0
    calls = _counting_fit(monkeypatch)
    store = fast.Checkpoints(tmp_path / "ck")
    research.walk_forward(changed, seal=seal, checkpoint=store)
    assert 0 < calls["n"] < store.hits + calls["n"]
    assert store.hits > 0


def test_corrupt_or_foreign_checkpoint_is_recomputed_not_trusted(tmp_path):
    store = fast.Checkpoints(tmp_path / "ck")
    key = "ab" + "0" * 62
    store.put(key, "m", predictions={"T": 0.6})
    path = tmp_path / "ck" / "ab" / f"{key}.json"
    path.write_text("{not json")
    assert store.get(key) is None
    path.write_text(json.dumps({"version": "other", "key": key, "predictions": {"T": 0.9}}))
    assert store.get(key) is None
    assert not list((tmp_path / "ck").rglob(".tmp-*"))


def test_checkpoint_key_covers_spec_rows_and_feature_values():
    rows, _ = _research_rows()
    store = fast.Checkpoints.__new__(fast.Checkpoints)
    store._digests = {}
    spec = research.MODELS["compact_offset"]
    base = store.key("compact_offset", spec, rows[:100], rows[100:120], rows[120:140])
    assert base == store.key("compact_offset", spec, rows[:100], rows[100:120], rows[120:140])
    assert base != store.key("compact_plus_flow", research.MODELS["compact_plus_flow"], rows[:100], rows[100:120], rows[120:140])
    assert base != store.key("compact_offset", spec, rows[:99], rows[99:120], rows[120:140])
    edited = dict(rows[5], features={**rows[5]["features"], "return_1m": 9.0})
    assert base != store.key("compact_offset", spec, rows[:5] + [edited] + rows[6:100], rows[100:120], rows[120:140])


def test_core_report_b_is_written_before_optional_sections_run(tmp_path, monkeypatch):
    def build(db):
        for i in range(12):
            _opening(db, i, 0.7, "UP", outcome=i % 2)
    path = _db(tmp_path, build)
    out = tmp_path / "out" / "opening_research.report.json"
    out.parent.mkdir()

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated hard failure in an optional phase")

    monkeypatch.setattr(research, "retrospective_rows", boom)
    monkeypatch.setattr("sys.argv", ["x", "--journal", str(path), "--output", str(out), "--retrospective",
                                     "--asof", ASOF.isoformat(), "--checkpoint-dir", str(tmp_path / "ck")])
    with pytest.raises(RuntimeError):
        research.main()
    report = json.loads(out.read_text())
    assert report["optional_sections_pending"] == ["retrospective"]
    assert report["asof"] == ASOF.isoformat() and "report_B_core" in report["phase_seconds"]
    assert (out.parent / "report_A_settled_audit.json").exists()


def test_pinned_asof_excludes_later_settlements_everywhere(tmp_path):
    _retro_journal(tmp_path / "r.db")
    with sqlite3.connect(tmp_path / "r.db") as db:
        early, _ = research.retrospective_rows(db, set(), asof=datetime(2026, 11, 2, 12, 20, tzinfo=timezone.utc))
        later, _ = research.retrospective_rows(db, set(), asof=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert [r["ticker"] for r in early] == ["KXBTC15M-R1"]
    assert sorted(r["ticker"] for r in later) == ["KXBTC15M-R1", "KXBTC15M-R2"]


# ------------------------------------------------------- Report A diagnostics


def test_quote_coverage_explains_missing_market_comparison(tmp_path):
    def no_table(db):
        _opening(db, 0, 0.7, "UP", outcome=1)
        db.execute("DROP TABLE opening_quote_snapshots")
    with _ro(_db(tmp_path, no_table)) as db:
        report = audit.audit(db, ASOF)
    assert report["quote_snapshot_coverage"]["table_present"] is False

    def post_open_only(db):
        _opening(db, 0, 0.7, "UP", outcome=1, late_quote=True)
    other = tmp_path / "b"
    other.mkdir()
    with _ro(_db(other, post_open_only)) as db:
        coverage = audit.audit(db, ASOF)["quote_snapshot_coverage"]
    assert coverage["rows"] == 1 and coverage["rows_at_or_before_open"] == 0
    assert coverage["settled_openings_with_preopen_quote"] == 0
    assert "only post-open" in coverage["reading"]


def test_holdout_no_call_reasons_and_daily_breakdown(tmp_path):
    def build(db):
        for i in range(40):
            if i < 32:
                _opening(db, i, 0.7, "UP", outcome=1)
            else:  # the last fifth: fallback openings that are never shown
                _opening(db, i, 0.52, "UNCLEAR", status={"status": "UNCALIBRATED"}, outcome=1)
    with _ro(_db(tmp_path, build)) as db:
        report = audit.audit(db, ASOF)
    holdout = report["holdout"]
    assert holdout["displayed_accuracy"]["displayed_calls"] == 0
    assert holdout["no_call_reasons"]["reasons"] == {"DIRECTION_UNCLEAR": 8}
    day = report["daily_breakdown"]["2026-10-01"]
    assert day["openings"] == 40 and day["displayed_calls"] == 32
    assert any(line.startswith("HOLDOUT_NO_CALL") for line in audit.summary_lines(report))


# ----------------------------------------------------------- model diagnostics


@pytest.fixture(scope="module")
def trained():
    rng = random.Random(400)
    names = ["return_1m", "return_5m", "return_15m", "volatility_change", "relative_volume", "body_fraction"]
    rows, start = [], datetime(2025, 3, 1, tzinfo=timezone.utc)
    for i in range(400):
        f = {n: rng.gauss(0, 1) for n in names}
        f["distance_scaled"] = rng.gauss(0, 1)
        bl = rng.gauss(0, 0.16)
        y = int(rng.random() < learning.sigmoid(bl + 1.2 * f["return_5m"]))
        o = start + timedelta(minutes=15 * i)
        rows.append(dict(ticker=f"KXBTC15M-D{i:04d}", timestamp=o.isoformat(), opens_at=o.isoformat(),
                         closes_at=(o + timedelta(minutes=15)).isoformat(),
                         label_available_at=(o + timedelta(minutes=16)).isoformat(), features=f, outcome=y,
                         baseline=learning.sigmoid(bl), market=None, remaining=900.0,
                         opening_delay_seconds=0.0, regime="x"))
    # The deployed model is a 4-component ensemble (logistic, ridge, boosted
    # stumps, volatility baseline); build exactly that shape from train/validation.
    pool = opening_models._pool(rows[:280], rows[280:360], total_contracts=opening_models.BOOST_MIN_CONTRACTS)
    ensemble = pool[-1][1]
    assert ensemble["kind"] == "opening_ensemble" and len(ensemble["components"]) == 4
    artifact = {"model_id": "test-ensemble", "model": ensemble,
                "labels_available_through": rows[359]["label_available_at"],
                "available_at": rows[360]["opens_at"]}
    return artifact, rows


def test_model_diagnostics_verify_components_inputs_and_contributions(trained):
    artifact, rows = trained
    rows = [dict(r, features=dict(r["features"])) for r in rows]
    for row in rows:
        row["recorded_up"] = opening_models.predict(artifact["model"], row["features"],
                                                    baseline_probability=row["baseline"])
        row["recorded_model"] = "opening-candidate/test-ensemble"
        row["features"].pop("body_fraction", None)  # an input that is never present at opening
    report = diag.diagnose(artifact, rows)
    assert report["status"] == "DIAGNOSED"
    assert report["component_count"] == 4 and report["active_components"] >= 1
    assert {c["kind"] for c in report["components"]} == {"logistic", "ridge", "opening_gradient_boosting", "volatility_baseline"}
    assert all(c["prediction_errors"] == 0 for c in report["components"])
    assert report["feature_sensitivity"]["top_20"][0][0] == "return_5m"  # the true signal
    assert "body_fraction" in report["input_coverage"]["never_present"]
    assert "body_fraction" in report["feature_sensitivity"]["no_effect_inputs"]
    assert set(report["component_drop_sensitivity"]) == {
        f"{i}:{m['kind']}" for i, m in enumerate(artifact["model"]["components"])}
    # recorded_up was computed before body_fraction was removed, so the
    # reproduction check must report mismatches rather than pass silently.
    rep = report["reproduction"]
    assert rep["openings_made_by_this_artifact"] == 400
    assert rep["exactly_reproduced_within_1e_6"] < 400 and rep["max_abs_difference"] > 1e-6
    assert rep["mismatches"] and rep["mismatches"][0]["difference"] == rep["max_abs_difference"]


def test_model_diagnostics_reproduce_recorded_probabilities_exactly(trained):
    artifact, rows = trained
    rows = [dict(r, features=dict(r["features"])) for r in rows]
    for i, row in enumerate(rows):
        made_by_artifact = i >= 360
        row["recorded_model"] = ("opening-candidate/test-ensemble" if made_by_artifact
                                 else "opening24h-zero-drift-final-average-v1")
        # Fallback openings carry a different (baseline) probability.
        row["recorded_up"] = (opening_models.predict(artifact["model"], row["features"],
                                                     baseline_probability=row["baseline"])
                              if made_by_artifact else row["baseline"])
    report = diag.diagnose(artifact, rows)
    rep = report["reproduction"]
    assert rep["openings_made_by_this_artifact"] == 40
    assert rep["exactly_reproduced_within_1e_6"] == 40 and rep["mismatches"] == []
    assert rep["openings_made_by_other_models"] == {"opening24h-zero-drift-final-average-v1": 360}
    oos = report["out_of_sample_for_this_artifact"]
    # Row 360 opened one minute before row 359's label arrived (the label
    # cutoff), so only rows 361-399 are out-of-sample for this artifact.
    assert oos["contracts"] == 39 and "artifact_better_log_loss" in oos


def test_regression_fallback_openings_are_not_reported_as_reproduction_failures(trained):
    """Phase 3 compared every opening after available_at, so openings made by
    the statistical fallback looked like ensemble reproduction failures."""
    artifact, rows = trained
    rows = [dict(r, features=dict(r["features"])) for r in rows[360:]]
    for row in rows:
        row["recorded_model"] = "opening24h-zero-drift-final-average-v1"
        row["recorded_up"] = row["baseline"]
    tampered = dict(rows[0], recorded_model="opening-candidate/test-ensemble", recorded_up=0.99)
    report = diag.diagnose(artifact, rows[1:] + [tampered])
    rep = report["reproduction"]
    assert rep["openings_made_by_this_artifact"] == 1
    assert [m["ticker"] for m in rep["mismatches"]] == [tampered["ticker"]]  # real defects still caught

"""Shadow rigorous opening evaluation: statistics, fitting, selection hygiene, safety.

All data here is SYNTHETIC and says nothing about live accuracy.
"""
import copy
import hashlib
import json
import math
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.signals_display import calibration, learning, opening_models
from src.signals_display import rigorous_evaluation as rigor
from src.signals_display import rigorous_synthetic as synthetic

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- statistics


@pytest.mark.parametrize(
    "t,df,expected",
    [
        (0.0, 5, 0.5),
        (2.0, 10, 0.9633062),
        (1.6973, 30, 0.95),
        (-2.0423, 30, 0.025),
        (3.0, 3, 0.9711656),
    ],
)
def test_student_t_cdf_matches_reference_values(t, df, expected):
    assert rigor.student_t_cdf(t, df) == pytest.approx(expected, abs=2e-4)


def test_student_t_cdf_agrees_with_scipy_when_available():
    stats = pytest.importorskip("scipy.stats")
    for df in (2, 7, 33, 120):
        for t in (-3.1, -0.4, 0.9, 2.6):
            assert rigor.student_t_cdf(t, df) == pytest.approx(stats.t.cdf(t, df), abs=1e-9)


def test_alpha_spending_bounds_all_repeated_evaluations():
    spent = [rigor.alpha_for_attempt(k) for k in range(1, 20001)]
    assert spent[0] == pytest.approx(0.05 * 6 / math.pi**2)
    assert all(a > b for a, b in zip(spent, spent[1:]))
    assert sum(spent) < rigor.FAMILY_ALPHA
    with pytest.raises(ValueError):
        rigor.alpha_for_attempt(0)


def test_paired_superiority_test_direction_and_null():
    rng = random.Random(4)
    reference = [rng.uniform(0.5, 0.9) for _ in range(60)]
    same = rigor.paired_superiority_test(reference, reference)
    assert same["p_value"] == 1.0
    better = rigor.paired_superiority_test([x - 0.08 + rng.gauss(0, 0.02) for x in reference], reference)
    worse = rigor.paired_superiority_test([x + 0.08 + rng.gauss(0, 0.02) for x in reference], reference)
    assert better["p_value"] < 0.001 < 0.999 < worse["p_value"]


# --------------------------------------------------------- convergence and L2


def _signal_rows(n, beta, seed=3):
    rows = synthetic.opening_rows(n, seed=seed, signal=beta)
    for r in rows:  # isolate one standardized regressor for coefficient checks
        r["features"] = {"return_5m": r["features"]["return_5m"] / 0.0009}
    return rows


def test_newton_fit_converges_and_recovers_known_coefficient():
    rows = _signal_rows(4000, 0.8)
    prep = learning.preprocess(rows)
    model = rigor.fit_logistic(rows, prep, 1e-6, use_offset=True)
    assert model["converged"] and model["newton_iterations"] < 20
    assert model["final_gradient_max_abs"] < 1e-6
    coefficient = model["weights"][0] / prep["scales"][0]
    assert coefficient == pytest.approx(0.8, abs=0.08)


def test_legacy_fixed_step_fit_is_under_converged_for_comparison():
    rows = _signal_rows(1500, 0.8)
    prep = learning.preprocess(rows)
    newton = rigor.fit_logistic(rows, prep, 1e-6, use_offset=False)
    legacy = learning.fit(rows, "logistic", 0.1, prep)
    # Documented audit finding F2: the legacy 220-step fit stops short.
    assert abs(legacy["weights"][0]) < abs(newton["weights"][0]) - 0.02


def test_l2_grid_is_meaningful_and_monotone():
    rows = _signal_rows(400, 0.5)
    prep = learning.preprocess(rows)
    norms = [
        abs(rigor.fit_logistic(rows, prep, l2, use_offset=True)["weights"][0])
        for l2 in rigor.LAMBDA_GRID
    ]
    assert all(a > b for a, b in zip(norms, norms[1:]))
    assert norms[0] - norms[-1] > 0.1
    legacy = [learning.fit(rows, "logistic", l2, prep)["weights"][0] for l2 in (0.1, 1.0)]
    assert abs(legacy[0] - legacy[1]) < 0.01  # audit finding: legacy grid nearly inert


# ---------------------------------------------------------------- calibration


def test_calibration_reuses_existing_regularized_platt():
    rng = random.Random(9)
    truth = [rng.gauss(0, 0.7) for _ in range(800)]
    ys = [int(rng.random() < learning.sigmoid(t)) for t in truth]
    overconfident = [learning.sigmoid(3 * t) for t in truth]
    platt = calibration.fit_platt(overconfident, ys)
    assert platt["slope"] == pytest.approx(1 / 3, abs=0.06)
    rows = synthetic.opening_rows(200, seed=11, signal=0.6)
    prep = rigor.compact_preprocessor(rows)
    model = rigor.fit_logistic(rows, prep, 3.0, use_offset=True)
    calibrated = rigor._calibrated(model, rows)
    direct = calibration.fit_platt(
        [rigor.raw_probability(model, r) for r in rows], [r["outcome"] for r in rows]
    )
    assert calibrated["calibration"]["method"] == "platt_monotone_v1"
    assert calibrated["calibration"]["slope"] == pytest.approx(direct["slope"])
    assert calibrated["calibration"]["intercept"] == pytest.approx(direct["intercept"])


# ------------------------------------------- selection without validation reuse


def test_calibration_ranking_and_l2_use_disjoint_chronological_rows(monkeypatch):
    rows = opening_models._validate_rows(synthetic.opening_rows(170, seed=21, signal=0.4))
    train, validation = rows[:100], rows[100:136]
    v1, v2 = rigor.split_validation(validation)
    assert {r["ticker"] for r in v1}.isdisjoint({r["ticker"] for r in v2})
    assert max(r["label_available_at"] for r in v1) <= min(r["timestamp"] for r in v2)
    calibration_sets, fit_sets = [], []
    original_calibrated, original_fit = rigor._calibrated, rigor.fit_logistic

    def spy_calibrated(model, subset):
        calibration_sets.append({r["ticker"] for r in subset})
        return original_calibrated(model, subset)

    def spy_fit(subset, *args, **kwargs):
        fit_sets.append({r["ticker"] for r in subset})
        return original_fit(subset, *args, **kwargs)

    monkeypatch.setattr(rigor, "_calibrated", spy_calibrated)
    monkeypatch.setattr(rigor, "fit_logistic", spy_fit)
    ranking, _ = rigor.build_candidates(train, v1, v2)
    v1_ids, v2_ids, train_ids = ({r["ticker"] for r in s} for s in (v1, v2, train))
    assert calibration_sets and all(s <= v1_ids for s in calibration_sets)
    assert fit_sets and all(s <= train_ids for s in fit_sets)
    assert all(item["ranking_metrics"]["samples"] == len(v2) for item in ranking)
    assert v2_ids.isdisjoint(set().union(*calibration_sets, *fit_sets))


# ------------------------------------ false approval, power, chronology, reuse


def test_noise_models_are_rarely_approved():
    approvals = [
        rigor.evaluate(
            synthetic.opening_rows(170, seed=100 + i), used_test_contracts=[], include_existing=False
        )["status"]
        == "SHADOW_PASSED_RIGOROUS_GATES"
        for i in range(20)
    ]
    assert sum(approvals) <= 1


def test_genuine_synthetic_signal_is_detected():
    reports = [
        rigor.evaluate(
            synthetic.opening_rows(1000, seed=200 + i, signal=1.0),
            used_test_contracts=[],
            include_existing=False,
        )
        for i in range(3)
    ]
    # Power is deliberately finite: ~200 untouched test contracts detect this
    # strong synthetic signal most, not all, of the time.
    assert sum(r["status"] == "SHADOW_PASSED_RIGOROUS_GATES" for r in reports) >= 2
    for r in reports:
        assert r["selected_before_test"] in rigor.CANDIDATES
        assert r["gate"]["point_estimates_beat_both_references"]
        assert all(item["converged"] for item in r["convergence"].values())


def test_chronology_forward_holdouts_and_alpha_spending():
    rows = synthetic.opening_rows(400, seed=31, signal=0.5)
    first = rigor.evaluate(rows, used_test_contracts=[], attempt=1, include_existing=False)
    chronology = first["chronology"]
    assert chronology["train_labels_through"] <= chronology["calibration_first_prediction"]
    assert chronology["calibration_labels_through"] <= chronology["ranking_first_prediction"]
    assert chronology["ranking_labels_through"] <= chronology["test_first_prediction"]
    more = rows + synthetic.opening_rows(
        60, seed=31, signal=0.5, start=opening_models.timestamp(rows[-1]["closes_at"])
    )
    for i, row in enumerate(more[400:]):
        row["ticker"] = f"KXBTC15M-LATER-{i:04d}"
    second = rigor.evaluate(
        more, used_test_contracts=first["test_contracts"], attempt=2, include_existing=False
    )
    assert second["alpha_spent"] < first["alpha_spent"]
    if second["test_exposed"]:
        assert set(second["test_contracts"]).isdisjoint(first["test_contracts"])
    blocked = rigor.evaluate(
        rows, used_test_contracts=first["test_contracts"], attempt=2, include_existing=False
    )
    assert blocked["status"] == "INSUFFICIENT_UNSEEN_FORWARD_HOLDOUT"
    assert not blocked["test_exposed"]


def test_existing_pipeline_is_compared_on_the_same_untouched_test():
    report = rigor.evaluate(synthetic.opening_rows(170, seed=41), used_test_contracts=[])
    comparison = report["existing_pipeline_comparison"]
    assert comparison["validation_reused_for_fit_and_rank"] is True
    assert comparison["test"]["samples"] == report["split_contracts"]["test"]
    assert "rigorous_gate_applied_to_existing" in comparison
    assert "confidence_bands" in comparison


# --------------------------------------------------------- high-confidence bands


def _band_rows(spec):
    rows, probabilities = [], []
    for p, n, correct in spec:
        for i in range(n):
            up = p >= 0.5
            outcome = int(up) if i < correct else int(not up)
            rows.append({"outcome": outcome})
            probabilities.append(p)
    return probabilities, rows


def test_confidence_bands_report_sample_size_coverage_and_uncertainty():
    probabilities, rows = _band_rows([(0.55, 100, 54), (0.35, 50, 32), (0.15, 40, 34), (0.08, 10, 9)])
    report = rigor.confidence_band_report(probabilities, rows)
    bands = report["bands"]
    assert bands["50-60%"]["contracts"] == 100
    assert bands["60-70%"]["contracts"] == 50 and bands["60-70%"]["down_calls"] == 50
    assert bands["80-90%"]["observed_accuracy"] == pytest.approx(34 / 40)
    assert bands["80-90%"]["status"] == "OBSERVED_ACCURACY_CONSISTENT_WITH_STATED_CONFIDENCE"
    assert bands["90%+"]["status"] == "INSUFFICIENT_SAMPLE"
    assert report["selective_abstention"][">=80%"]["contracts"] == 50
    assert report["selective_abstention"][">=80%"]["coverage"] == pytest.approx(50 / 200)
    # 43/50 = 86% observed, but the Wilson lower bound is below 80%.
    assert report["high_confidence_target_demonstrated"] is False


def test_high_confidence_target_requires_lower_bound_at_or_above_target():
    probabilities, rows = _band_rows([(0.93, 300, 285)])
    report = rigor.confidence_band_report(probabilities, rows)
    assert report["selective_abstention"][">=90%"]["accuracy_wilson_95"][0] > 0.8
    assert report["high_confidence_target_demonstrated"] is True
    probabilities, rows = _band_rows([(0.93, 300, 200)])
    report = rigor.confidence_band_report(probabilities, rows)
    assert report["bands"]["90%+"]["status"] == "STATED_CONFIDENCE_MISCALIBRATED"
    assert report["high_confidence_target_demonstrated"] is False


# ---------------------------------------------------------------- shadow safety


def test_report_is_shadow_only_and_cannot_load_as_an_opening_artifact():
    rows = synthetic.opening_rows(400, seed=51, signal=1.0)
    original = copy.deepcopy(rows)
    report = rigor.evaluate(rows, used_test_contracts=[], include_existing=False)
    assert rows == original
    for flag in ("promotion_allowed", "activation_allowed", "automatic_activation",
                 "active_model_changed", "execution_eligible"):
        assert report[flag] is False
    assert report["role"] == "SHADOW_ONLY"
    with pytest.raises((ValueError, KeyError, TypeError)):
        opening_models.validate_artifact(report)


def _empty_journal(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE forecast_outcomes (ticker TEXT,origin TEXT,observed_at TEXT,outcome INTEGER)")
        db.execute(
            "CREATE TABLE btc_engine_opening (mode TEXT,ticker TEXT,generated_at TEXT,cutoff TEXT,"
            "closes_at TEXT,up REAL,status TEXT,payload TEXT)"
        )


def test_cli_reads_journal_read_only_and_writes_only_its_own_files(tmp_path):
    journal = tmp_path / "signals_monitor.db"
    _empty_journal(journal)
    before = hashlib.sha256(journal.read_bytes()).hexdigest()
    output, ledger = tmp_path / "out.report.json", tmp_path / "ledger.json"
    command = [sys.executable, "-m", "src.signals_display.rigorous_evaluation",
               "--journal", str(journal), "--output", str(output), "--ledger", str(ledger)]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before
    assert json.loads(output.read_text())["format"] == rigor.REPORT_FORMAT
    assert json.loads(ledger.read_text())["format"] == rigor.LEDGER_FORMAT


def test_cli_refuses_to_overwrite_foreign_artifacts_or_registries(tmp_path):
    journal = tmp_path / "signals_monitor.db"
    _empty_journal(journal)
    artifact = tmp_path / "opening_shadow.json"
    artifact.write_text(json.dumps({"format": "kxbtc-opening-shadow-v1", "model": {}}))
    preserved = artifact.read_text()
    result = subprocess.run(
        [sys.executable, "-m", "src.signals_display.rigorous_evaluation", "--journal", str(journal),
         "--output", str(artifact), "--ledger", str(tmp_path / "ledger.json")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0 and artifact.read_text() == preserved
    registry_state = tmp_path / "registry.json"
    registry_state.write_text(json.dumps({"versions": []}))
    result = subprocess.run(
        [sys.executable, "-m", "src.signals_display.rigorous_evaluation", "--journal", str(journal),
         "--output", str(tmp_path / "out.json"), "--ledger", str(registry_state)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert json.loads(registry_state.read_text()) == {"versions": []}

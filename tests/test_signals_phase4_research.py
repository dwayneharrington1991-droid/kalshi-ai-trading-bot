"""Phase 4: comparisons against the running model, never mixing model versions."""
import math

import pytest

from src.signals_display import opening_research as research
from src.signals_display import rigorous_synthetic as synthetic
from tests.test_signals_opening_research import _augment

CURRENT = "opening-candidate/current-model"


def test_exact_mcnemar_matches_hand_computation():
    # Running model right on 6 contracts the candidate got wrong; never the reverse.
    result = research._mcnemar([False] * 6 + [True] * 4, [True] * 10)
    assert (result["candidate_only_right"], result["current_only_right"]) == (0, 6)
    assert result["p_value"] == pytest.approx(2 / 64)
    assert research._mcnemar([True, False], [True, False])["p_value"] == 1.0


def test_interactions_are_products_of_frozen_values_and_missing_stays_missing():
    rows = [{"features": {"return_15m": 0.2, "regime_rising": 1.0, "regime_falling": 0.0,
                          "opening_ema9_21_gap": None}}]
    research.add_interactions(rows)
    f = rows[0]["features"]
    assert f["ix_return_15m_x_rising"] == pytest.approx(0.2) and f["ix_return_15m_x_falling"] == 0.0
    assert f["ix_ema9_21_gap_x_rising"] is None
    assert "momentum_trend_volatility" in research.MODELS


def _rows(n=40):
    rows = []
    for i in range(n):
        y = i % 2
        rows.append({"ticker": f"T{i}", "outcome": y, "recorded_model": CURRENT if i >= 10 else "older-model",
                     "recorded_up": 0.7 if (i % 4 != 3) == bool(y) else 0.3,
                     "recorded_direction": None, "recorded_confidence_status": "EXPERIMENTAL CALIBRATED CANDIDATE",
                     "quote": None, "opens_at": f"2026-11-0{1 + i // 20}T00:00:00+00:00"})
        r = rows[-1]
        r["recorded_direction"] = "UP" if r["recorded_up"] >= 0.5 else "DOWN"
        if i % 5 == 0:
            r["recorded_direction"] = "WAIT"  # not displayed
    return rows


def test_running_model_comparison_uses_only_its_own_records_and_same_contracts():
    rows = _rows()
    perfect = {r["ticker"]: 0.9 if r["outcome"] else 0.1 for r in rows}
    partial = {r["ticker"]: 0.6 for r in rows[:30]}  # misses some running-model contracts: skipped
    report = research.current_model_comparison(rows, {"perfect": perfect, "partial": partial}, CURRENT)
    mine = [r for r in rows if r["recorded_model"] == CURRENT]
    called = [r for r in mine if r["recorded_direction"] in {"UP", "DOWN"}]
    assert report["holdout_openings_by_current_model"] == len(mine) == 30
    assert report["current_model_displayed_calls"] == len(called)
    assert report["current_model"]["calls"] == len(called)
    assert set(report["candidates"]) == {"perfect"}
    same = report["candidates"]["perfect"]["same_contracts"]
    assert same["calls"] == len(called) and same["accuracy"] == 1.0
    assert same["paired_vs_current"]["current_only_right"] == 0
    matched = report["candidates"]["perfect"]["matched_coverage"]
    assert matched["calls"] == len(called)  # same number of calls as the running model


def test_running_model_comparison_reports_absence_instead_of_mixing_versions():
    rows = [dict(r, recorded_model="older-model") for r in _rows()]
    report = research.current_model_comparison(rows, {}, CURRENT)
    assert report["status"] == "NO_CURRENT_MODEL_CALLS_IN_HOLDOUT"
    assert report["holdout_openings_by_current_model"] == 0


def test_running_model_comparison_uses_canonical_displayed_call_validation():
    rows = _rows()
    mine = [row for row in rows if row["recorded_model"] == CURRENT]
    mine[0]["recorded_direction"] = "DOWN" if mine[0]["recorded_up"] >= 0.5 else "UP"
    mine[1]["recorded_up"] = float("nan")
    predictions = {"candidate": {row["ticker"]: 0.7 for row in rows}}
    report = research.current_model_comparison(rows, predictions, CURRENT)
    expected = sum(research._displayable_recorded_call(row) for row in mine)
    assert report["current_model_displayed_calls"] == expected
    assert report["current_model"]["calls"] == expected
    assert "paired_mcnemar_multiple_testing" in report


def test_research_compares_candidates_with_running_model_on_sealed_holdout_only():
    rows = _augment(synthetic.opening_rows(420, seed=505, signal=0.6))
    for i, r in enumerate(rows):
        r["recorded_model"] = CURRENT
        r["recorded_up"] = 0.62 if i % 3 else 0.38
        r["recorded_direction"] = "UP" if r["recorded_up"] > 0.5 else "DOWN"
        r["recorded_confidence_status"] = "EXPERIMENTAL CALIBRATED CANDIDATE"
    report = research.research(rows, current_model=CURRENT)
    versus = report["versus_running_model"]
    assert versus["status"] == "MEASURED"
    assert versus["holdout_openings_by_current_model"] == report["sealed_holdout"]["holdout_contracts"]
    assert "momentum_trend_volatility" in versus["candidates"]
    family = report["multiple_testing"]["results"]
    assert any(k.startswith("momentum_trend_volatility vs") for k in family)
    for comparison in report["model_comparison"].values():
        assert "threshold_table_holdout" not in comparison
        assert "development_threshold_table" in comparison
        assert "holdout_by_regime" not in comparison

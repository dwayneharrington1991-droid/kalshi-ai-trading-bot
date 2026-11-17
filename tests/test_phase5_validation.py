"""Phase 5 evidence-layer regression tests (research only)."""
from pathlib import Path

from src.signals_display import settled_audit as audit
from tests.test_signals_settled_audit import ASOF, _db, _opening, _ro


ROOT = Path(__file__).resolve().parents[1]


def _claimed_scoreboard_db(tmp_path, mixed_models=False):
    outcomes = [1, 0, 0] + [1] * 19 + [0] * 7

    def build(db):
        for index, outcome in enumerate(outcomes):
            _opening(db, index, 0.70, "UP", outcome=outcome)
        if mixed_models:
            db.execute(
                "UPDATE btc_engine_opening SET payload=json_set(payload,'$.model','older') "
                "WHERE ticker='KXBTC15M-0000'"
            )

    return _db(tmp_path, build)


def test_phase5_reconciles_exact_claim_and_recent_window_without_hiding_uncertainty(tmp_path):
    with _ro(_claimed_scoreboard_db(tmp_path)) as db:
        report = audit.audit(db, ASOF, current_model="m1")
    phase5 = report["phase5_validation"]
    claim = phase5["scoreboard_claim_reconciliation"]
    evidence = phase5["minimum_evidence"]

    assert claim["overall_exact_match"] is True
    assert claim["recent_exact_match"] is True
    assert claim["running_model_present"] is True
    assert claim["running_model_observed"]["correct"] == 20
    assert evidence["current_directional_calls"] == 29
    assert evidence["eligible_frozen_openings"] == 29
    assert evidence["accuracy_wilson_95"][0] < 0.51
    assert evidence["conservative_calls_for_95pct_interval_half_width"] == {
        "10_percentage_points": 93,
        "7_5_percentage_points": 167,
        "5_percentage_points": 381,
    }
    assert phase5["recommendation"] == "BLOCKED_FOR_PROMOTION"
    assert "INSUFFICIENT_SETTLED_OPENINGS_FOR_MODEL_PROMOTION_RESEARCH" in phase5["blockers"]


def test_phase5_separates_model_versions_and_rejects_stale_score_claim(tmp_path):
    with _ro(_claimed_scoreboard_db(tmp_path, mixed_models=True)) as db:
        phase5 = audit.audit(db, ASOF, current_model="m1")["phase5_validation"]
    claim = phase5["scoreboard_claim_reconciliation"]
    assert claim["overall_exact_match"] is True  # arithmetic can match while scope is unsafe
    assert claim["scope_warning"] == "OVERALL_SCOREBOARD_MIXES_MODEL_VERSIONS"
    assert claim["running_model_observed"]["calls"] == 28
    assert "OVERALL_SCOREBOARD_MIXES_MODEL_VERSIONS" in phase5["blockers"]


def test_phase5_threshold_frontier_is_development_only_and_never_selects_policy(tmp_path):
    with _ro(_claimed_scoreboard_db(tmp_path)) as db:
        phase5 = audit.audit(db, ASOF)["phase5_validation"]
    research = phase5["development_threshold_research"]
    pooled = research["pooled_reference"]
    assert pooled["development_only"] is True
    assert pooled["selection_allowed"] is False
    assert pooled["optimal_threshold"] is None
    assert set(pooled["comparisons"]) == {
        f"{value:.2f}" for value in audit.PHASE4_THRESHOLD_CHALLENGERS
    }
    assert all("additional_calls_vs_0_55" in row for row in pooled["comparisons"].values())
    # Candidate/model cohorts receive separate frontiers; they are never pooled silently.
    assert set(research["by_model"]) == {"m1"}


def test_phase5_does_not_infer_profitability_without_executable_quotes(tmp_path):
    with _ro(_claimed_scoreboard_db(tmp_path)) as db:
        evidence = audit.audit(db, ASOF)["phase5_validation"]["profitability_evidence"]
    assert evidence["market_probability_comparison_status"] == "NOT_MEASURABLE"
    assert evidence["after_cost_counterfactual_status"] == "NOT_MEASURABLE"
    assert evidence["actual_fills_available"] is False
    assert "cannot be concluded" in evidence["conclusion"]


def test_oracle_runner_is_read_only_and_passes_running_model_to_report_a():
    script = (ROOT / "deploy/oracle/research_readonly.sh").read_text()
    report_a = script.split("# Report A per running service:", 1)[0]
    assert '--audit-only \\\n  ${current_model:+--current-model "$current_model"}' in report_a
    forbidden = ("systemctl restart", "systemctl stop", "kill ", "sqlite3 ",
                 "--promote", "place_order", "cancel_order")
    assert not any(token in script for token in forbidden)


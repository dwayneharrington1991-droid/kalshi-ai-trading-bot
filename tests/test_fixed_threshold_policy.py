import copy

import pytest

from src.signals_display import model_lifecycle as lc
from src.signals_display import opening_models as om
from tests.test_signals_opening_models import eligible_candidate


def _fixed(candidate, threshold=om.LIVE_DECISION_THRESHOLD, validation_threshold=0.7):
    artifact = copy.deepcopy(candidate)
    artifact["decision_policy"] = {"kind": "fixed_live_threshold", "threshold": threshold,
                                   "validation_selected_threshold": validation_threshold,
                                   "selected_before_untouched_test": True, "otherwise": "WAIT"}
    return artifact


def test_live_threshold_is_the_reviewed_55_and_matches_the_lifecycle_gate():
    assert om.LIVE_DECISION_THRESHOLD == 0.55
    assert lc.LIVE_THRESHOLD == om.LIVE_DECISION_THRESHOLD


def test_legacy_validation_selected_artifacts_keep_their_original_rule():
    legacy = eligible_candidate()
    selected = legacy["report"]["selective_signal_evaluation"]["selected_threshold"]
    legacy["decision_policy"] = {"kind": "validation_selected_symmetric_abstention", "threshold": selected,
                                 "selected_before_untouched_test": True, "otherwise": "WAIT"}
    om.validate_artifact(legacy)            # unchanged rule still accepts the legacy shape


def test_fixed_policy_at_exactly_0_55_is_accepted_with_the_validation_value_kept_for_audit():
    artifact = _fixed(eligible_candidate(), validation_threshold=0.7)
    om.validate_artifact(artifact)
    assert artifact["decision_policy"]["threshold"] == 0.55
    assert artifact["decision_policy"]["validation_selected_threshold"] == 0.7


@pytest.mark.parametrize("threshold", [0.54, 0.56, 0.6, 0.7])
def test_fixed_policy_at_any_other_threshold_is_rejected(threshold):
    with pytest.raises(ValueError, match="differs from the live decision threshold"):
        om.validate_artifact(_fixed(eligible_candidate(), threshold=threshold))


def test_challenger_with_a_non_55_validation_threshold_is_no_longer_rejected_by_the_gate():
    artifact = _fixed(eligible_candidate(), validation_threshold=0.7)
    decision, reasons = lc.gate(artifact, "x" * 64, {"markets": 0}, 0, 1)
    assert decision != "REJECTED_THRESHOLD_CHANGE"

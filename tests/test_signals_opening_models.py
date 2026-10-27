"""Opening model identity, display neutrality, immutable as-of features and learning."""
from copy import deepcopy
from datetime import timedelta
import json
import math

import pytest

from src.signals_display import prediction_engine as engine
from src.signals_display.monitor import Journal
from tests.test_signals_display import NOW
from tests.test_signals_prediction_engine import history, market, points


def opening(**kwargs):
    return engine.opening_forecast(
        market(),
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points(),
        NOW + timedelta(seconds=2),
        **kwargs,
    )


@pytest.mark.parametrize("p", [0.5, 0.5000000000000507, 0.4999999999, 0.50049, 0.49951])
def test_display_ties_are_neutral_without_changing_raw_probabilities(p):
    assert engine.direction_label(p) == "NEUTRAL"
    assert engine.display_probability_pair(p) == {
        "up": 50.0,
        "down": 50.0,
        "precision_percentage_points": 0.1,
    }
    old = {"model": "opening24h-zero-drift-final-average-v1", "up": p, "direction": "DOWN"}
    before = deepcopy(old)
    audit = engine.opening_diagnostics(old)
    assert audit["display_direction"] == "NEUTRAL" and audit["raw_probability_up"] == p
    assert old == before


def test_real_direction_and_complementary_rounding_remain_visible():
    assert engine.direction_label(0.47805) == "DOWN"
    assert engine.display_probability_pair(0.47805)["up"] == 47.8
    assert engine.direction_label(0.50051) == "UP"
    assert engine.direction_label(None) == "UNCLEAR"
    assert engine.display_probability_pair(float("nan"))["up"] is None


def test_opening_fallback_explains_model_and_feature_role_not_constant_probability():
    result = opening(
        audit_context={
            "official_contracts": 80,
            "minimum_contracts": 170,
            "artifact_status": "No trained artifact",
        }
    )
    audit = engine.opening_diagnostics(result, {"learning_status": {"active": "ensemble"}})
    assert audit["model_id"] == "opening24h-zero-drift-final-average-v1"
    assert not audit["opening_ensemble_active"] and audit["live_ensemble_active"]
    assert "80/170" in audit["promotion_blocker"]
    assert not audit["constant_probability"]
    assert result["probability_inputs"]["hard_coded_probability"] is False
    assert audit["history_available_before_open"] and audit["frozen_feature_count"] > 15
    assert result["up"] == pytest.approx(
        0.5 * (1 + math.erf(result["probability_inputs"]["z_score"] / math.sqrt(2)))
    )


def test_frozen_features_exclude_future_reference_and_microstructure_context():
    valid = opening()
    future = engine.opening_forecast(
        market(),
        history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)),
        points() + [(NOW + timedelta(milliseconds=1), 999999)],
        NOW + timedelta(seconds=20),
        frozen_context={
            "cutoff": (NOW + timedelta(milliseconds=1)).isoformat(),
            "candidate_features": {"micro_5s_velocity_dollars_per_second": 999999},
        },
    )
    assert future["up"] == valid["up"]
    assert future["frozen_features"]["values"] == valid["frozen_features"]["values"]
    assert (
        future["frozen_features"]["optional_context_status"]
        == "POST_OPENING_OR_UNVERIFIABLE_CONTEXT_EXCLUDED"
    )
    assert future["frozen_features"]["max_input_received_at"] <= NOW.isoformat()


def test_asof_microstructure_is_recorded_as_candidate_not_probability_boost():
    baseline = opening()
    result = opening(
        frozen_context={
            "cutoff": NOW.isoformat(),
            "primary_source": "Coinbase BTC/USD stream",
            "candidate_features": {"micro_5s_velocity_dollars_per_second": 2.0},
            "sources": {
                "Coinbase BTC/USD stream": {
                    "windows": {
                        "5s": {
                            "status": "READY",
                            "last_received_at": (NOW - timedelta(milliseconds=500)).isoformat(),
                        }
                    }
                },
                "Kraken BTC/USD stream": {
                    "status": "LIVE",
                    "windows": {
                        "5s": {
                            "status": "READY",
                            "features": {"depth_imbalance": 0.5},
                            "last_received_at": (NOW - timedelta(milliseconds=500)).isoformat(),
                        }
                    },
                },
                "Kraken futures stream": {
                    "status": "LIVE",
                    "latest_price": 100010,
                    "data_age_seconds": 0.8,
                },
            },
        }
    )
    assert result["up"] == baseline["up"]
    f = result["frozen_features"]
    assert f["values"]["micro_5s_velocity_dollars_per_second"] == 2.0
    assert f["values"]["spot_depth_imbalance"] == 0.5
    assert f["values"]["derivatives_basis"] is not None
    assert f["max_input_received_at"] == (NOW - timedelta(milliseconds=500)).isoformat()


def test_new_opening_features_remain_immutable_after_restart(tmp_path):
    journal = Journal(tmp_path / "j.db")
    first = opening()
    saved = journal.opening_record(
        first["ticker"], "PUBLIC MARKET DATA", first, NOW + timedelta(seconds=2)
    )
    changed = deepcopy(first)
    changed["frozen_features"]["values"]["return_1m"] = 1000
    changed["up"] = 0.9
    assert (
        journal.opening_record(
            first["ticker"], "PUBLIC MARKET DATA", changed, NOW + timedelta(seconds=3)
        )
        == saved
    )
    assert Journal(tmp_path / "j.db").opening_record(first["ticker"], "PUBLIC MARKET DATA") == saved


def test_opening_dataset_uses_only_frozen_feature_records_and_official_outcomes(tmp_path):
    from src.signals_display import opening_models

    journal = Journal(tmp_path / "j.db")
    original = opening()
    journal.opening_record(
        original["ticker"], "PUBLIC MARKET DATA", original, NOW + timedelta(seconds=2)
    )
    journal.settle(
        original["ticker"], "yes", "PUBLIC MARKET DATA", (NOW + timedelta(minutes=16)).isoformat()
    )
    with journal.read() as db:
        rows = opening_models.dataset(db, NOW + timedelta(minutes=17))
    assert len(rows) == 1 and rows[0]["features"] == {
        name: value
        for name, value in original["frozen_features"]["values"].items()
        if value is not None
    }
    assert rows[0]["market"] is None and rows[0]["timestamp"] == original["generated_at"]
    candidate, report = opening_models.evaluate(rows)
    assert candidate is None and report["status"] == "INSUFFICIENT_OPENING_LABELS"
    assert not report["active_model_changed"] and not report["promotion_allowed"]
    with journal.connect() as db:
        for index, mutate in enumerate(("future", "missing")):
            clone = deepcopy(original)
            clone["ticker"] = f"KXBTC15M-{index}"
            if mutate == "future":
                clone["frozen_features"]["max_input_received_at"] = (
                    NOW + timedelta(seconds=1)
                ).isoformat()
            else:
                clone.pop("frozen_features")
            db.execute(
                "INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                (
                    "PUBLIC MARKET DATA",
                    clone["ticker"],
                    clone["generated_at"],
                    clone["data_cutoff"],
                    clone["closes_at"],
                    clone["up"],
                    clone["status"],
                    json.dumps(clone),
                ),
            )
            db.execute(
                "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                (clone["ticker"], "official", (NOW + timedelta(minutes=16)).isoformat(), 1),
            )
    with journal.read() as db:
        assert len(opening_models.dataset(db, NOW + timedelta(minutes=17))) == 1


def synthetic_rows(n=200):
    rows = []
    for i in range(n):
        start = NOW - timedelta(days=10) + timedelta(minutes=15 * i)
        y = i % 2
        rows.append(
            dict(
                ticker=f"KXBTC15M-{i}",
                timestamp=start.isoformat(),
                opens_at=start.isoformat(),
                closes_at=(start + timedelta(minutes=15)).isoformat(),
                label_available_at=(start + timedelta(minutes=15, seconds=1)).isoformat(),
                features={"distance_scaled": (2 * y - 1) * 0.001, "return_1m": (2 * y - 1) * 0.01},
                outcome=y,
                baseline=0.5,
                market=None,
                remaining=900,
                regime="test",
            )
        )
    return rows


def test_opening_selection_is_frozen_before_untouched_test_outcomes():
    from src.signals_display import opening_models

    rows = synthetic_rows()
    _candidate, report = opening_models.evaluate(rows)
    changed = deepcopy(rows)
    for row in changed[160:]:
        row["outcome"] = 1 - row["outcome"]
    _other, future = opening_models.evaluate(changed)
    assert report["trained"] and report["split_contracts"][0] >= 100
    assert report["selected_before_test"] == future["selected_before_test"]
    assert report["validation"] == future["validation"]
    assert report["test"]["selected_candidate"] != future["test"]["selected_candidate"]
    assert not report["promotion_allowed"]
    _, blocked = opening_models.evaluate(rows, used_test_contracts=report["test_contracts"])
    assert blocked["status"] == "HOLDOUT_REUSE_BLOCKED"


def test_gradient_boosting_is_bounded_and_shadow_only():
    from src.signals_display import opening_models
    from src.signals_display.learning import preprocess

    rows = synthetic_rows(120)
    model = opening_models.fit_boost(rows, preprocess(rows), rounds=3)
    assert len(model["stumps"]) <= 3
    p = opening_models.predict(model, rows[0]["features"], baseline_probability=0.5)
    assert 0 <= p <= 1


def eligible_candidate():
    from src.signals_display import opening_models

    candidate, report = opening_models.evaluate(synthetic_rows(), used_test_contracts=set())
    assert candidate is not None and report["status"] == "SHADOW_CANDIDATE_PASSED_STATISTICAL_GATES"
    candidate.update(
        created_at=(NOW - timedelta(seconds=3)).isoformat(),
        available_at=(NOW - timedelta(seconds=2)).isoformat(),
        runtime_loaded_at=(NOW - timedelta(seconds=1)).isoformat(),
    )
    return candidate


def test_only_opening_target_artifact_available_before_open_can_be_used():
    from src.signals_display import opening_models

    candidate = eligible_candidate()
    result = opening(candidate_artifact=candidate)
    assert result["model"] == "opening-candidate/" + candidate["model_id"]
    assert (
        result["model_selection"]["validation_status"]
        == "SHADOW_CANDIDATE_PASSED_STATISTICAL_GATES"
    )
    assert result["up"] == opening_models.predict(
        candidate["model"],
        result["frozen_features"]["values"],
        baseline_probability=result["baseline_probability_up"],
    )
    for field in ("available_at", "runtime_loaded_at"):
        late = {**candidate, field: (NOW + timedelta(seconds=1)).isoformat()}
        rejected = opening(candidate_artifact=late)
        assert rejected["model"] == "opening24h-zero-drift-final-average-v1"
        assert "OPENING_ARTIFACT_INELIGIBLE" in rejected["model_selection"]["candidate_rejection"]
        assert rejected["up"] == rejected["baseline_probability_up"]
    incompatible = {**candidate, "prediction_target": "LIVE_HORIZON"}
    assert (
        opening(candidate_artifact=incompatible)["model"]
        == "opening24h-zero-drift-final-average-v1"
    )


def test_selected_candidate_keeps_independent_baseline_for_future_training(tmp_path):
    from src.signals_display import opening_models

    journal = Journal(tmp_path / "j.db")
    result = opening(candidate_artifact=eligible_candidate())
    journal.opening_record(
        result["ticker"], "PUBLIC MARKET DATA", result, NOW + timedelta(seconds=2)
    )
    journal.settle(
        result["ticker"], "yes", "PUBLIC MARKET DATA", (NOW + timedelta(minutes=16)).isoformat()
    )
    with journal.read() as db:
        rows = opening_models.dataset(db, NOW + timedelta(minutes=17))
        score_payload = json.loads(
            db.execute("SELECT payload FROM btc_engine_predictions").fetchone()[0]
        )
    assert rows[0]["baseline"] == result["baseline_probability_up"]
    assert rows[0]["baseline"] != result["up"]
    assert score_payload["baseline_up"] == result["baseline_probability_up"]


def test_neutral_forecasts_are_not_directional_accuracy_wins():
    rows = [
        {"up": 0.5000000000000507, "outcome": 1, "score_payload": {}},
        {"up": 0.4998, "outcome": 0, "score_payload": {}},
    ]
    result = engine._evaluate_rows(rows)
    assert result["samples"] == 2 and result["directional_samples"] == 0
    assert result["unclear"] == 2 and result["correct"] == 0 and result["accuracy"] is None
    assert result["brier"] is not None and result["log_loss"] is not None

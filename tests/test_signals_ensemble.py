"""Independent ensembles, chronological isolation, immutable offline model versions."""
from copy import deepcopy
from datetime import timedelta
import json
import subprocess
import sys

import pytest

from src.signals_display import learning
from src.signals_display.model_registry import ModelRegistry, atomic_json
from tests.test_signals_display import NOW


def linear(kind="logistic", intercept=0.0):
    return {
        "kind": kind,
        "weights": [0.0, 0.0, intercept],
        "preprocessor": {"names": ["distance_scaled"], "means": [0.0], "scales": [1.0]},
        "calibration": [1.0, 0.0],
        "l2": 1.0,
    }


def model():
    return {
        "kind": "ensemble",
        "components": [linear(), linear("ridge", 0.3), {"kind": "volatility_baseline"}],
        "mixture_weights": [0.2, 0.3, 0.5],
        "requires_independent_baseline": True,
    }


def artifact(identity="mock1"):
    return {
        "format": "kxbtc-independent-v2",
        "model": model(),
        "model_id": identity,
        "labels_available_through": (NOW - timedelta(days=1)).isoformat(),
        "report": {
            "status": "EXPERIMENTAL_CANDIDATE",
            "trained": True,
            "split_contracts": [100, 30, 30],
        },
        "validated": False,
    }


def rows(count=180):
    result = []
    for i in range(count):
        at = NOW + timedelta(minutes=15 * i)
        result.append(
            dict(
                ticker=f"C{i:03}",
                timestamp=at.isoformat(),
                closes_at=(at + timedelta(minutes=10)).isoformat(),
                label_available_at=(at + timedelta(minutes=11)).isoformat(),
                features={"distance_scaled": (-1) ** i * 0.001, "return_1m": (-1) ** i * 0.0001},
                outcome=i % 2,
                baseline=0.5,
                market=0.5,
                remaining=600,
            )
        )
    return result


def test_ensemble_requires_independent_baseline_and_never_uses_market_odds():
    m = model()
    expected = 0.2 * 0.5 + 0.3 * 0.3 + 0.5 * 0.8
    assert learning.predict(
        m, {"distance_scaled": 1, "kalshi_odds": 0.01}, baseline_probability=0.8
    ) == pytest.approx(expected)
    assert learning.predict(
        m, {"distance_scaled": 1, "kalshi_odds": 0.99}, baseline_probability=0.8
    ) == pytest.approx(expected)
    with pytest.raises(ValueError, match="independent event-aware"):
        learning.predict(m, {})
    for bad in (float("nan"), float("inf"), True, -0.1, 1.1):
        with pytest.raises(ValueError):
            learning.predict(m, {}, baseline_probability=bad)


def test_ensemble_weights_and_calibration_do_not_see_future_test_labels():
    data = rows()
    _, original = learning.compare(data)
    changed = deepcopy(data)
    for row in changed[144:]:
        row["outcome"] = 1 - row["outcome"]
        row["features"]["return_1m"] *= 1000
    _, altered = learning.compare(changed)
    assert original["selected_before_test"] == altered["selected_before_test"]
    assert original["candidate_validation"] == altered["candidate_validation"]
    assert original["ensemble_weights"] == altered["ensemble_weights"]
    assert original["validation_end"] < original["test_first_prediction"]
    ensemble = next(c for c in original["candidate_validation"] if c["kind"] == "ensemble")
    assert sum(ensemble["mixture_weights"]) == pytest.approx(1)
    assert original["test_uncertainty"]["contracts"] == 36
    assert len(original["test_uncertainty"]["brier_interval"]) == 2
    assert original["edge_status"] == "edge unverified"


def test_holdout_exposure_blocks_reuse_before_fitting(monkeypatch):
    data = rows()
    monkeypatch.setattr(
        learning,
        "fit",
        lambda *args: pytest.fail("Already exposed test labels must not be fitted/evaluated again"),
    )
    candidate, report = learning.compare(data, used_test_contracts={"C179"})
    assert candidate is None and report["status"] == "HOLDOUT_REUSE_BLOCKED"
    assert report["holdout_overlap_contracts"] == 1 and report["holdout_reuse_checked"]


def test_json_loader_validates_all_ensemble_members_and_weights(tmp_path):
    path = tmp_path / "model.json"
    good = artifact()
    atomic_json(path, good)
    loaded, reason = learning.load_artifact(path)
    assert loaded == good and "experimental" in reason
    variants = []
    bad = deepcopy(good)
    bad["model"]["components"][0]["preprocessor"]["names"] = ["kalshi_odds"]
    variants.append(bad)
    bad = deepcopy(good)
    bad["model"]["mixture_weights"] = [-0.1, 0.3, 0.8]
    variants.append(bad)
    bad = deepcopy(good)
    bad["model"]["components"][0] = deepcopy(good["model"])
    variants.append(bad)
    bad = deepcopy(good)
    bad["model"]["components"][0]["weights"] = [True, 0.0, 1.0]
    variants.append(bad)
    bad = deepcopy(good)
    bad["model"]["components"][0]["kind"] = "pickle"
    variants.append(bad)
    for item in variants:
        atomic_json(path, item)
        loaded, reason = learning.load_artifact(path)
        assert loaded is None and reason.startswith("Invalid artifact rejected")


def test_registry_versions_are_immutable_explicit_and_rollback_preserves_history(tmp_path):
    registry = ModelRegistry(tmp_path / "registry")
    output = tmp_path / "active.json"
    journal = tmp_path / "existing-journal.db"
    journal.write_bytes(b"original history")
    a, b = artifact("one"), artifact("two")
    first = registry.register(a, NOW)
    second = registry.register(b, NOW + timedelta(seconds=1))
    assert registry.state()["active_version"] is None and not output.exists()
    assert registry.register(a, NOW) == first
    registry.activate(first, output, NOW)
    assert json.loads(output.read_text()) == a
    registry.activate(second, output, NOW + timedelta(seconds=2))
    assert json.loads(output.read_text()) == b
    assert registry.rollback(output, NOW + timedelta(seconds=3)) == first
    assert json.loads(output.read_text()) == a
    assert registry.read(second) == b
    assert journal.read_bytes() == b"original history"
    assert registry.state()["activations"][-1]["action"] == "ROLLBACK"


def test_registry_refuses_corrupted_or_future_model_without_changing_active(tmp_path):
    registry = ModelRegistry(tmp_path / "registry")
    output = tmp_path / "active.json"
    output.write_text('{"keep":"existing"}')
    item = artifact()
    version = registry.register(item, NOW)
    stored = registry.versions / (version + ".json")
    changed = deepcopy(item)
    changed["model"]["mixture_weights"] = [0.1, 0.4, 0.5]
    atomic_json(stored, changed)
    with pytest.raises(ValueError, match="checksum"):
        registry.activate(version, output, NOW)
    assert output.read_text() == '{"keep":"existing"}'
    with pytest.raises(ValueError, match="24-character"):
        registry.read("../../active")
    item["labels_available_through"] = (NOW + timedelta(days=1)).isoformat()
    version = registry.register(item, NOW)
    with pytest.raises(ValueError, match="Future-trained"):
        registry.activate(version, output, NOW)


def test_registry_schedule_and_exposure_ledger(tmp_path):
    registry = ModelRegistry(tmp_path / "registry")
    assert registry.due(NOW)
    report = {
        "status": "BASELINE_RETAINED",
        "test_contracts": ["KXBTC15M-A", "KXBTC15M-B"],
        "test_fingerprint": "mock",
        "protocol": learning.PROTOCOL,
    }
    registry.record_evaluation(report, NOW)
    assert not registry.due(NOW + timedelta(hours=23))
    assert registry.due(NOW + timedelta(hours=24))
    assert registry.used_test_contracts() == {"KXBTC15M-A", "KXBTC15M-B"}
    with registry.update_lock():
        with pytest.raises(ValueError, match="already running"):
            with ModelRegistry(registry.directory).update_lock():
                pytest.fail("Concurrent update acquired same holdout")


def test_scheduled_insufficient_data_preserves_active_artifact_and_journal(tmp_path):
    from src.signals_display.monitor import Journal

    journal = Journal(tmp_path / "journal.db")
    output = tmp_path / "signals_model.json"
    output.write_text('{"existing":"preserve"}')
    before = (tmp_path / "journal.db").read_bytes()
    cmd = [
        sys.executable,
        "-m",
        "src.signals_display.learning",
        "--journal",
        str(journal.path),
        "--output",
        str(output),
        "--registry",
        str(tmp_path / "registry"),
        "--scheduled",
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0 and "INSUFFICIENT_OFFICIAL_LABELS" in completed.stdout
    assert output.read_text() == '{"existing":"preserve"}'
    assert (tmp_path / "journal.db").read_bytes() == before
    again = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert again.returncode == 0 and "MODEL_UPDATE_NOT_DUE" in again.stdout


def test_eighty_official_results_gate_before_reading_historical_payloads(tmp_path):
    from src.signals_display.monitor import Journal

    journal = Journal(tmp_path / "journal.db")
    with journal.connect() as db:
        db.executemany(
            "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
            [(f"KXBTC15M-{i}", "official", NOW.isoformat(), i % 2) for i in range(80)],
        )
    with journal.read() as db:
        queries = []
        db.set_trace_callback(queries.append)
        candidate, report, loaded = learning.prepare_comparison(db, NOW)
    assert candidate is None and loaded == []
    assert report["official_contracts"] == 80 and not report["dataset_loaded"]
    assert report["status"] == "INSUFFICIENT_OFFICIAL_LABELS"
    assert not any("payload" in q.lower() or "from predictions" in q.lower() for q in queries)


def test_dataset_projects_small_features_and_does_not_invent_market_benchmark(tmp_path):
    from src.signals_display.monitor import Journal

    journal = Journal(tmp_path / "journal.db")
    old = NOW - timedelta(hours=1)
    payload = {
        "stale": False,
        "market": {"closes_at": (old + timedelta(minutes=15)).isoformat(), "quotes": {}},
        "model": {
            "candidate_features": {"values": {"distance_scaled": 0.1, "return_1m": 0.01}},
            "baseline_probability_up": 0.6,
        },
        "raw_unused_book": "x" * 1000000,
    }
    with journal.connect() as db:
        db.execute(
            "INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
            ("KXBTC15M-MOCK", "official", (old + timedelta(minutes=16)).isoformat(), 1),
        )
        db.execute(
            "INSERT INTO predictions(mode,ticker,timestamp,payload,settlement_origin,settlement_up,settled_at) VALUES (?,?,?,?,?,?,?)",
            (
                "PUBLIC MARKET DATA",
                "KXBTC15M-MOCK",
                old.isoformat(),
                json.dumps(payload),
                "official",
                1,
                (old + timedelta(minutes=16)).isoformat(),
            ),
        )
    with journal.read() as db:
        queries = []
        db.set_trace_callback(queries.append)
        result = learning.dataset(db, NOW)
    assert len(result) == 1 and result[0]["market"] is None
    assert result[0]["features"] == payload["model"]["candidate_features"]["values"]
    assert learning.market_score(result)["samples"] == 0
    assert len(json.dumps(result)) < 2000
    assert not any("select payload" in q.lower() for q in queries)

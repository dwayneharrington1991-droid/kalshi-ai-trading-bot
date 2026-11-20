import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import model_lifecycle as lc
from src.signals_display import post_promotion as pw
from src.signals_display import promotion as pr

T_PROMO = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
SHA = "a" * 64
OTHER = "b" * 64


def _good(sha=SHA, latency=120.0):
    return {"health": "HEALTHY", "ready": "READY", "signals_only": True, "workers_healthy": True,
            "latency_ms": latency, "quote_age_s": None, "model_file_sha": sha}


def _watch(**extra):
    base = {"state": "WATCHING", "candidate_checksum": "c" * 64, "candidate_file": "x", "promoted_file_sha": SHA,
            "previous_checksum": "p" * 64, "backup_file": "y", "promoted_at": T_PROMO.isoformat(),
            "consecutive_bad": 0, "cycles": 0}
    base.update(extra)
    return base


def _rows(n, after=True):
    rows = []
    for i in range(n):
        at = T_PROMO + timedelta(minutes=15 * (i + 1)) if after else T_PROMO - timedelta(minutes=15 * (i + 1))
        rows.append({"label_available_at": at.isoformat(), "outcome": i % 2, "features": [0.0], "baseline": 0.5})
    return rows


class _Art(dict):
    pass


def test_served_model_with_a_different_file_sha_rolls_back_at_once():
    decision, reason, _, _ = pw.evaluate(_watch(), [_good(sha=OTHER)], promoted_artifact=None,
                                         previous_artifact=None, rows=[])
    assert decision == "ROLLBACK_REQUIRED"
    assert "does not match" in reason


def test_two_consecutive_operational_failures_roll_back_but_one_does_not():
    bad = _good(latency=pw.MAX_LATENCY_MS + 1)
    decision, _, _, updated = pw.evaluate(_watch(), [bad], promoted_artifact=None, previous_artifact=None, rows=[])
    assert decision == "WATCHING" and updated["consecutive_bad"] == 1
    decision, reason, _, _ = pw.evaluate(_watch(), [bad, bad], promoted_artifact=None, previous_artifact=None, rows=[])
    assert decision == "ROLLBACK_REQUIRED" and "consecutive operational" in reason


@pytest.mark.parametrize("field,value", [
    ("health", "DEGRADED"), ("ready", "NOT_READY"), ("signals_only", False), ("workers_healthy", False),
    ("latency_ms", None), ("quote_age_s", pw.MAX_QUOTE_AGE_S + 1),
])
def test_each_operational_check_is_enforced(field, value):
    sample = {**_good(), field: value}
    assert pw.operational_bad(sample, SHA)


def test_a_missing_sample_counts_as_a_failure():
    assert pw.operational_bad(None, SHA)


def test_latency_within_budget_is_not_a_failure():
    assert not pw.operational_bad(_good(latency=pw.MAX_LATENCY_MS), SHA)


def test_too_few_post_promotion_markets_never_produce_a_performance_decision():
    decision, _, evidence, _ = pw.evaluate(_watch(), [_good()], promoted_artifact=_Art(model={}),
                                           previous_artifact=_Art(model={}), rows=_rows(10))
    assert decision == "WATCHING"
    assert evidence["post_promotion_markets"] == 10
    assert "SUSPENDED" in evidence["performance"]


def test_rows_labelled_before_the_switch_are_never_counted(monkeypatch):
    monkeypatch.setattr(pw.opening_models, "predict", lambda model, f, baseline_probability: 0.5)
    rows = _rows(200, after=False)
    decision, _, evidence, _ = pw.evaluate(_watch(), [_good()], promoted_artifact=_Art(model={}),
                                           previous_artifact=_Art(model={}), rows=rows)
    assert evidence["post_promotion_markets"] == 0
    assert decision == "WATCHING"


def test_a_significantly_worse_promoted_model_rolls_back(monkeypatch):
    monkeypatch.setattr(pw.opening_models, "predict",
                        lambda model, f, baseline_probability: model["p"])
    promoted, previous = _Art(model={"p": 0.02}), _Art(model={"p": 0.5})
    decision, reason, evidence, _ = pw.evaluate(_watch(), [_good()], promoted_artifact=promoted,
                                                previous_artifact=previous, rows=_rows(120))
    assert decision == "ROLLBACK_REQUIRED"
    assert "significantly worse" in reason
    assert evidence["logloss_gain_z"] <= -pw.Z_DEGRADE


def test_a_clearly_better_promoted_model_is_not_rolled_back_and_passes(monkeypatch):
    monkeypatch.setattr(pw.opening_models, "predict", lambda model, f, baseline_probability: 0.5 + 0.4 * model["sign"])
    rows = _rows(pw.WATCH_MAX_MARKETS)
    for r in rows:
        r["outcome"] = 1
    promoted, previous = _Art(model={"sign": 1}), _Art(model={"sign": 0})
    decision, reason, evidence, _ = pw.evaluate(_watch(), [_good()], promoted_artifact=promoted,
                                                previous_artifact=previous, rows=rows)
    assert decision == "PASSED"
    assert evidence["logloss_gain_z"] > 0


def test_calibration_worse_than_previous_rolls_back(monkeypatch):
    monkeypatch.setattr(pw.opening_models, "predict", lambda model, f, baseline_probability: 0.5)
    values = iter([0.3, 0.0])                      # promoted ECE, then previous ECE
    monkeypatch.setattr(lc, "_ece", lambda pairs, bins=5: next(values))
    decision, reason, _, _ = pw.evaluate(_watch(), [_good()], promoted_artifact=_Art(model={}),
                                         previous_artifact=_Art(model={}), rows=_rows(pw.WATCH_MIN_PAIRED))
    assert decision == "ROLLBACK_REQUIRED"
    assert "calibration" in reason


def test_watch_state_round_trips_and_finishes(tmp_path):
    record = pw.start_watch(tmp_path, candidate_checksum="c", candidate_file="cf", promoted_file_sha=SHA,
                            previous_checksum="p", backup_file="bf", promoted_at=T_PROMO.isoformat())
    assert pw.read_watch(tmp_path)["state"] == "WATCHING"
    done = pw.finish_watch(tmp_path, record, "PASSED", "ok")
    assert pw.read_watch(tmp_path)["state"] == "PASSED"
    assert done["reason"] == "ok"


def test_watch_file_is_private(tmp_path):
    pw.start_watch(tmp_path, candidate_checksum="c", candidate_file="cf", promoted_file_sha=SHA,
                   previous_checksum="p", backup_file="bf", promoted_at=T_PROMO.isoformat())
    mode = (tmp_path / pw.WATCH_FILE).stat().st_mode & 0o777
    assert mode == 0o600


class _Activator:
    enabled = True

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def rollback(self, artifact_path, checksum):
        self.calls.append((str(artifact_path), checksum))
        return {"ok": self.ok, "detail": "x"}


def test_a_rollback_restores_the_previous_model_in_active_json(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / pr.ACTIVE).write_text(json.dumps({"candidate_checksum": "c" * 64, "training_cutoff": "2026-11-01T00:00:00+00:00",
                                               "backup_file": "bk.json"}))
    backup = state / "backups"
    backup.mkdir()
    watch = pw.start_watch(state, candidate_checksum="c" * 64, candidate_file=state / "cf.json", promoted_file_sha=SHA,
                           previous_checksum="p" * 64, backup_file=backup / "live-x.json", promoted_at=T_PROMO.isoformat())
    monkeypatch.setattr(pw, "evaluate", lambda *a, **k: ("ROLLBACK_REQUIRED", "test reason", {}, watch))
    activator = _Activator(ok=True)
    history = pr.History(state)
    result = pr._advance_watch(state, activator, watch, [_good()], None, history, lambda e, d: None, T_PROMO)
    assert result["result"] == "ROLLED_BACK"
    active = json.loads((state / pr.ACTIVE).read_text())
    assert active["candidate_checksum"] == "p" * 64           # the restored model is now the champion
    assert active["rolled_back_from"] == "c" * 64
    assert pw.read_watch(state)["state"] == "ROLLED_BACK"
    assert history.entries()[-1]["event"] == "ROLLED_BACK"
    assert history.verify_chain()


def test_an_unverified_rollback_blocks_all_promotion(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    watch = pw.start_watch(state, candidate_checksum="c" * 64, candidate_file=state / "cf.json", promoted_file_sha=SHA,
                           previous_checksum="p" * 64, backup_file=state / "b.json", promoted_at=T_PROMO.isoformat())
    monkeypatch.setattr(pw, "evaluate", lambda *a, **k: ("ROLLBACK_REQUIRED", "test", {}, watch))
    history = pr.History(state)
    result = pr._advance_watch(state, _Activator(ok=False), watch, [_good()], None, history, lambda e, d: None, T_PROMO)
    assert result["result"] == "BLOCKED"
    assert pw.read_watch(state)["state"] == "ROLLBACK_FAILED"
    again = pr._advance_watch(state, _Activator(ok=True), pw.read_watch(state), [_good()], None, history,
                              lambda e, d: None, T_PROMO)
    assert again["result"] == "BLOCKED"

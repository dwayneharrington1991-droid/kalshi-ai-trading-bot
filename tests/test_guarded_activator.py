"""Guarded activator: authorization, verification, atomic pointer, health-checked switch, restore. Synthetic."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.signals_display import guarded_activator as ga
from src.signals_display import model_lifecycle as lc
from tests.test_model_lifecycle import good_candidate, bundle, CUTOFF

HEALTHY = {"health": "HEALTHY", "ready": "READY", "signals_only": True}
CONFIG = {"restart": "guarded-deploy", "version": 1}


def authorize(path, config=CONFIG):
    Path(path).write_text(ga.AUTH_PREFIX + ga.activator_checksum(config) + "\n")


def make(tmp_path, *, authorized=True, restarter=lambda p: True, probe=lambda: HEALTHY, window=lambda now: True):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    auth = tmp_path / "auth.txt"
    if authorized:
        authorize(auth)
    pointer = tmp_path / "live_model_pointer.json"
    pointer.write_text(json.dumps({"checksum": "old" * 21 + "x", "artifact": "old.json"}))
    return ga.GuardedActivator(state, pointer, authorization_file=auth, config=CONFIG,
                               restarter=restarter, probe=probe, window_safe=window), pointer


def test_disabled_without_the_exact_authorization(tmp_path):
    act, pointer = make(tmp_path, authorized=False)
    assert act.enabled is False
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), status["candidate_checksum"])
    assert res["ok"] is False and "ACTIVATION_DISABLED" in res["detail"]
    assert json.loads(pointer.read_text())["checksum"].startswith("old")          # live pointer untouched


def test_wrong_authorization_text_is_not_accepted(tmp_path):
    (tmp_path / "auth.txt").write_text("AUTHORIZE-AUTO-PROMOTION:" + "0" * 64)
    act, _ = make(tmp_path, authorized=False)
    assert act.enabled is False


def test_refuses_without_a_guarded_restart_path(tmp_path):
    act, pointer = make(tmp_path, restarter=None)
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), status["candidate_checksum"])
    assert res == {"ok": False, "detail": "RESTART_PATH_NOT_CONFIGURED"}
    assert json.loads(pointer.read_text())["checksum"].startswith("old")


def test_successful_switch_replaces_the_pointer_atomically(tmp_path):
    act, pointer = make(tmp_path)
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), body["checksum"])
    assert res["ok"] is True
    assert json.loads(pointer.read_text())["checksum"] == body["checksum"]
    assert not (tmp_path / "live_model_pointer.next").exists()                  # no partial leftovers


def test_failed_post_switch_health_restores_the_previous_model(tmp_path):
    states = iter([HEALTHY, {"health": "DEGRADED", "ready": "NOT_READY", "signals_only": True}])
    act, pointer = make(tmp_path, probe=lambda: next(states, {"health": "DEGRADED"}))
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), body["checksum"])
    assert res["ok"] is False and "restored" in res["detail"] or "previous" in res["detail"]


def test_refuses_a_tampered_or_undeployable_artifact(tmp_path):
    act, pointer = make(tmp_path)
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    f = Path(status["candidate_file"])
    obj = json.loads(f.read_text()); obj["artifact"]["decision_policy"]["threshold"] = 0.99
    f.write_text(json.dumps(obj))
    res = act.activate(f, body["checksum"])
    assert res["ok"] is False and "artifact refused" in res["detail"]
    assert json.loads(pointer.read_text())["checksum"].startswith("old")


def test_refuses_to_switch_inside_a_protected_opening_window(tmp_path):
    act, pointer = make(tmp_path, window=lambda now: False)
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), body["checksum"])
    assert res == {"ok": False, "detail": "inside a protected opening window"}


def test_refuses_when_the_live_service_is_not_healthy_before_switching(tmp_path):
    act, pointer = make(tmp_path, probe=lambda: {"health": "DEGRADED"})
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res = act.activate(Path(status["candidate_file"]), body["checksum"])
    assert res["ok"] is False and "not HEALTHY" in res["detail"]


def test_activator_never_touches_orders_services_or_ports():
    code = Path(ga.__file__).read_text()
    for forbidden in ("place_order", "create_order", "cancel_order", "systemctl", "pkill", "kill(", "8765", "8767",
                      "DELETE FROM", "DROP TABLE"):
        assert forbidden not in code, forbidden

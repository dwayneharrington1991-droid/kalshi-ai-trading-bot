"""Guarded activation: isolated rehearsal of activation, failure, rollback and crash recovery, plus
unit rules for authorization, the atomic pointer, restart safety and the runner's activator choice."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.signals_display import activation_rehearsal as rh
from src.signals_display import guarded_activator as ga
from src.signals_display import lifecycle_runner as lr
from src.signals_display import model_lifecycle as lc
from src.signals_display import promotion as pr

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rehearsal(tmp_path_factory):
    out = rh.Harness(tmp_path_factory.mktemp("rehearsal")).run()
    return out


def test_isolated_rehearsal_passes_every_scenario(rehearsal):
    failed = [k for k, v in rehearsal["scenarios"].items() if not v["pass"]]
    assert rehearsal["result"] == "PASS", failed
    assert rehearsal["live_service_touched"] is False
    assert set(rehearsal["scenarios"]) == {"S1_success_activation", "S2_failed_activation_restores_previous",
                                           "S3_automatic_rollback", "S4_crash_recovery_no_double_promotion",
                                           "S5_crash_before_switch_recovers", "S6_unknown_live_model_blocks",
                                           "S7_no_authorization_no_change", "S8_not_qualified_never_promoted"}


def _activator(tmp_path, *, authorized=True, restarter=lambda p: True, probe=None, window=True):
    config = {"restart": "x", "port": 1}
    auth = tmp_path / "auth.txt"
    if authorized:
        auth.write_text(ga.AUTH_PREFIX + ga.activator_checksum(config))
    probe = probe or (lambda: {"health": "HEALTHY", "ready": "READY", "signals_only": True, "model_file_sha": None})
    return ga.GuardedActivator(tmp_path, authorization_file=auth, config=config, restarter=restarter,
                               probe=probe, window_safe=lambda now: window, sleep=lambda s: None,
                               clock=lambda: datetime(2026, 11, 19, 3, 4, tzinfo=timezone.utc))


def test_authorization_must_match_the_exact_configuration_checksum(tmp_path):
    config = {"restart": "x", "port": 1}
    auth = tmp_path / "auth.txt"
    auth.write_text(ga.AUTH_PREFIX + ga.activator_checksum({"restart": "x", "port": 2}))
    act = ga.GuardedActivator(tmp_path, authorization_file=auth, config=config, restarter=lambda p: True,
                              probe=lambda: {}, sleep=lambda s: None)
    assert act.enabled is False
    auth.write_text("AUTHORIZE-AUTO-PROMOTION:" + "0" * 64)
    assert ga.GuardedActivator(tmp_path, authorization_file=auth, config=config, restarter=lambda p: True,
                               probe=lambda: {}).enabled is False


def test_no_restarter_or_probe_means_disabled_even_when_authorized(tmp_path):
    assert _activator(tmp_path, restarter=None).enabled is False
    assert ga.GuardedActivator(tmp_path, authorization_file=tmp_path / "auth.txt", config={"a": 1}, restarter=lambda p: True,
                               probe=None).enabled is False


def test_pointer_is_replaced_atomically_and_never_left_partial(tmp_path):
    act = _activator(tmp_path)
    act._write_pointer({"checksum": "a", "file_sha": "1", "artifact": "x"})
    assert not (tmp_path / "live_model_pointer.json.next").exists()
    assert json.loads((tmp_path / ga.POINTER).read_text())["checksum"] == "a"


def test_window_guard_refuses_a_switch_inside_an_opening_window(tmp_path):
    act = _activator(tmp_path, window=False)
    assert act.activate(tmp_path / "cand.json", "x") == {"ok": False, "detail": "inside a protected opening window"}


def test_probe_rejects_a_service_reporting_the_wrong_model(tmp_path):
    act = _activator(tmp_path)
    act._write_pointer({"checksum": "old", "file_sha": "a" * 64, "artifact": "p"})
    act.probe = lambda: {"health": "HEALTHY", "ready": "READY", "signals_only": True, "model_file_sha": "b" * 64}
    act.restarter = lambda p: True
    art = tmp_path / "x.json"
    art.write_text("{}")
    res = act._switch_to({"checksum": "new", "file_sha": "c" * 64, "artifact": str(art)})
    assert res is False                                          # healthy is not enough: the model must match


def test_runner_only_builds_a_guarded_activator_from_a_rehearsal_restart_config(tmp_path):
    import argparse
    cfg = tmp_path / "restart.json"
    args = argparse.Namespace(restart_config=cfg, state_dir=tmp_path, port=8768)
    assert lr.build_guarded(args) is None                        # no config: disabled
    cfg.write_text(json.dumps({"kind": "production", "port": 28001, "argv": ["p", "--artifact", "a"], "cwd": str(tmp_path),
                               "env_file": str(tmp_path / "e.json"), "log": str(tmp_path / "l")}))
    act = lr.build_guarded(args)
    assert act is not None and act.enabled is False              # production without authorization: disabled
    cfg.write_text(json.dumps({"kind": "rehearsal", "port": 8768, "pidfile": "p", "log": "l"}))
    assert lr.build_guarded(args) is None                        # never the live port
    cfg.write_text(json.dumps({"kind": "rehearsal", "port": 28002, "pidfile": str(tmp_path / "p"), "log": str(tmp_path / "l")}))
    act = lr.build_guarded(args)
    assert act is not None and act.enabled is False              # still needs an authorization file


def test_production_restart_config_needs_authorization_and_never_uses_retired_ports(tmp_path):
    import argparse
    cfg = tmp_path / "restart.json"
    args = argparse.Namespace(restart_config=cfg, state_dir=tmp_path, port=8768)
    prod = {"kind": "production", "port": 28010, "argv": ["python", "-m", "x", "--artifact", "a", "--port", "28010"],
            "cwd": str(tmp_path), "env_file": str(tmp_path / "env.json"), "log": str(tmp_path / "log")}
    cfg.write_text(json.dumps(dict(prod, port=8765)))
    assert lr.build_guarded(args) is None                       # retired port: refused
    cfg.write_text(json.dumps(dict(prod, port=8767)))
    assert lr.build_guarded(args) is None
    cfg.write_text(json.dumps(prod))
    act = lr.build_guarded(args)
    assert act is not None and act.enabled is False              # no authorization file: disabled
    from src.signals_display import production_restart as prs
    config = prs.auth_config(dict(prod, kind="production"))
    (tmp_path / "promotion-authorization.txt").write_text(ga.AUTH_PREFIX + ga.activator_checksum(config))
    assert lr.build_guarded(args).enabled is True                # exact authorization: enabled


def test_restarter_never_signals_a_process_it_did_not_start(tmp_path):
    import os
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        (tmp_path / "pid").write_text(str(victim.pid))
        rh.ProcessRestarter(28003, tmp_path / "pid", tmp_path / "log")._stop()
        assert victim.poll() is None                             # no rehearsal marker: left alone
    finally:
        victim.kill()


def test_activation_code_never_names_live_ports_orders_or_services():
    code = "".join(Path(ROOT / "src/signals_display" / f).read_text() for f in
                   ("guarded_activator.py", "activation_rehearsal.py"))
    for forbidden in ("place_order", "create_order", "cancel_order", "systemctl", "pkill", "os.killpg",
                      "DELETE FROM", "DROP TABLE", "8765", "8767", "8768"):
        assert forbidden not in code, forbidden


def test_activator_refuses_a_tampered_candidate_before_any_switch(tmp_path):
    act = _activator(tmp_path)
    act._write_pointer({"checksum": "old", "file_sha": "a" * 64, "artifact": "p"})
    status, body = rh.store_candidate(tmp_path / "store", rh.make_artifact("tamper-test"), datetime(2026, 11, 1, tzinfo=timezone.utc))
    f = Path(status["candidate_file"])
    obj = json.loads(f.read_text()); obj["artifact"]["decision_policy"]["threshold"] = 0.99
    f.write_text(json.dumps(obj))
    res = act.activate(f, status["candidate_checksum"])
    assert res["ok"] is False and "candidate refused" in res["detail"]
    assert json.loads((tmp_path / ga.POINTER).read_text())["checksum"] == "old"   # pointer untouched


def test_activator_refuses_when_the_live_service_is_unhealthy_before_switching(tmp_path):
    act = _activator(tmp_path, probe=lambda: {"health": "DEGRADED", "ready": "NOT_READY", "signals_only": True})
    act._write_pointer({"checksum": "old", "file_sha": "a" * 64, "artifact": "p"})
    status, _ = rh.store_candidate(tmp_path / "store", rh.make_artifact("unhealthy-test"), datetime(2026, 11, 1, tzinfo=timezone.utc))
    res = act.activate(Path(status["candidate_file"]), status["candidate_checksum"])
    assert res["ok"] is False and "not HEALTHY" in res["detail"]
    assert json.loads((tmp_path / ga.POINTER).read_text())["checksum"] == "old"

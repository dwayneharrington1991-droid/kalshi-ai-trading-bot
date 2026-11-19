"""Production restart integration, tested in isolation: a stand-in process launched exactly like the
live service (captured command line, cwd, environment file). Nothing touches the live 8768 service."""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src.signals_display import activation_rehearsal as rh
from src.signals_display import guarded_activator as ga
from src.signals_display import lifecycle_runner as lr
from src.signals_display import production_restart as pr_
from src.signals_display import promotion as pr

ROOT = Path(__file__).resolve().parents[1]


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(tmp_path):
    """A stand-in 'live' service started the way production is launched, plus its captured launch."""
    port = _port()
    art_a = tmp_path / "model_a.json"
    art_b = tmp_path / "model_b.json"
    art_a.write_text(json.dumps({"v": "a"}))
    art_b.write_text(json.dumps({"v": "b", "x": 1}))
    argv = [sys.executable, "-m", "src.signals_display.activation_rehearsal", "serve",
            "--port", str(port), "--artifact", str(art_a), "--marker", rh.MARKER]
    proc = subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(100):
        if pr_._listener_pid(port) == proc.pid:
            break
        import time; time.sleep(0.05)
    captured, reason = pr_.capture(port)
    assert captured is not None, reason
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"PATH": os.environ["PATH"], "HOME": str(tmp_path)}))
    restarter = pr_.ProductionRestarter(captured, env_file, tmp_path / "service.log")
    yield {"port": port, "proc": proc, "art_a": art_a, "art_b": art_b, "captured": captured,
           "restarter": restarter, "tmp": tmp_path}
    for p in (pr_._listener_pid(port),):
        if p:
            try:
                os.kill(p, 15)
            except OSError:
                pass
    proc.kill()


def test_capture_records_the_exact_launch_and_refuses_systemd(live, monkeypatch):
    assert live["captured"]["argv"][-6:][0] == "--port" or "--artifact" in live["captured"]["argv"]
    assert live["captured"]["cwd"] == str(ROOT)
    monkeypatch.setattr(pr_, "_cgroup_is_systemd", lambda pid: True)
    assert pr_.capture(live["port"]) == (None, "process is systemd-managed; manual restart is refused")


def test_same_launch_compares_everything_except_the_artifact_value():
    tpl = ["py", "-m", "x", "--artifact", "A", "--port", "1"]
    assert pr_.same_launch(["py", "-m", "x", "--artifact", "B", "--port", "1"], tpl)
    assert not pr_.same_launch(["py", "-m", "x", "--artifact", "B", "--port", "2"], tpl)
    assert not pr_.same_launch(["py", "-m", "other", "--artifact", "B", "--port", "1"], tpl)


def test_restart_switches_the_served_model_and_keeps_the_launch_identical(live):
    before_pid = pr_._listener_pid(live["port"])
    assert lr.probe_live(live["port"])["model_file_sha"] == ga.file_sha(live["art_a"])
    assert live["restarter"](live["art_b"]) is True
    assert lr.probe_live(live["port"])["model_file_sha"] == ga.file_sha(live["art_b"])
    assert pr_._listener_pid(live["port"]) != before_pid           # a new process, same port
    assert pr_.same_launch(pr_._cmdline(pr_._listener_pid(live["port"])), live["captured"]["argv"])


def test_restart_refuses_a_process_that_does_not_match_the_capture(live):
    mismatched = dict(live["captured"], argv=live["captured"]["argv"] + ["--extra-flag"])
    restarter = pr_.ProductionRestarter(mismatched, live["tmp"] / "env.json", live["tmp"] / "service.log")
    pid = pr_._listener_pid(live["port"])
    assert restarter(live["art_b"]) is False
    assert pr_._listener_pid(live["port"]) == pid                 # untouched


def test_the_restart_never_escalates_to_sigkill():
    code = (ROOT / "src/signals_display/production_restart.py").read_text()
    assert "SIGKILL" not in code and "kill -9" not in code
    assert "signal.SIGTERM" in code


def test_full_activation_and_restore_through_the_real_activator_on_the_stand_in(live):
    from datetime import datetime, timezone
    from tests.test_model_lifecycle import bundle, good_candidate, CUTOFF
    state = live["tmp"] / "state"
    state.mkdir()
    config = {"restart": "production-process", "port": live["port"]}
    auth = state / "promotion-authorization.txt"
    auth.write_text(ga.AUTH_PREFIX + ga.activator_checksum(config))
    act = ga.GuardedActivator(state, authorization_file=auth, config=config, restarter=live["restarter"],
                              probe=lambda: lr.probe_live(live["port"]), clock=lambda: datetime(2026, 11, 19, 3, 4, tzinfo=timezone.utc),
                              sleep=lambda s: None)
    act.bootstrap(live["art_a"])
    status, body = bundle(state, good_candidate(), CUTOFF)
    cand_sha = ga.file_sha(status["candidate_file"])
    res = act.activate(Path(status["candidate_file"]), body["checksum"])
    assert res["ok"] is True, res
    assert lr.probe_live(live["port"])["model_file_sha"] == cand_sha
    # failed post-switch verification: the restarter starts the previous model again
    res_rb = act.rollback(live["art_a"], "production:" + ga.file_sha(live["art_a"]))
    assert res_rb["ok"] is True
    assert lr.probe_live(live["port"])["model_file_sha"] == ga.file_sha(live["art_a"])


def test_capture_cli_writes_private_config_and_refuses_retired_ports(live, tmp_path):
    cfg, env_out, log = tmp_path / "restart.json", tmp_path / "env.json", tmp_path / "service.log"
    run = subprocess.run([sys.executable, "-m", "src.signals_display.production_restart", "--port", str(live["port"]),
                          "--config-out", str(cfg), "--env-out", str(env_out), "--log", str(log)],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    assert "environment_values" in run.stdout and "PATH" not in run.stdout        # no environment values printed
    data = json.loads(cfg.read_text())
    assert data["kind"] == "production" and data["argv"] == live["captured"]["argv"]
    assert oct(cfg.stat().st_mode & 0o777) == "0o600" and oct(env_out.stat().st_mode & 0o777) == "0o600"
    retired = subprocess.run([sys.executable, "-m", "src.signals_display.production_restart", "--port", "8765",
                              "--config-out", str(cfg), "--env-out", str(env_out), "--log", str(log)],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert retired.returncode != 0 and "retired port" in retired.stderr

"""Phase 5 deploy orchestrator: failed-install recovery, exact environment
restore, exclusive lock. Real processes on 8768/8765/8767 with fake services;
the orchestrator runs unmodified from a temporary git checkout."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PORTS = (8768, 8765, 8767)

FAKE_SERVER = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
a = sys.argv[1:]
val = lambda f, d=None: a[a.index(f) + 1] if f in a else d
port, build = int(val("--port")), val("--build-commit", "old")
health_file = val("--health-file")
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        health = open(health_file).read().strip() if health_file and os.path.exists(health_file) else "HEALTHY"
        body = {"/healthz": {"status": health, "build_commit": build, "signals_only": True},
                "/api/state": {"decision_intelligence": {"execution_capability": False}}}.get(p)
        if p == "/api/data-quality-alerts" and "--alerts" in a:
            body = {"read_only": True, "status": "OK", "alerts": []}
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode(); self.send_response(200); self.end_headers(); self.wfile.write(data)
HTTPServer(("127.0.0.1", port), H).serve_forever()
'''

FAKE_SS = r'''#!/bin/bash
port=$(sed -n 's/.*sport = :\([0-9]*\).*/\1/p' <<<"$*")
pid=$(pgrep -f "fake_server.py .*--port $port( |$)" | head -1)
[[ -n $pid ]] && echo "LISTEN 0 5 127.0.0.1:$port 0.0.0.0:* users:((\"python\",pid=$pid,fd=3))"
exit 0
'''

# Fake installer: SIM_INSTALL selects the behaviour. It only ever touches 8768.
FAKE_INSTALLER = r'''#!/bin/bash
set -u
sha=$1
source "$(dirname "$0")/deploy/oracle/preview_deploy_lock.sh"
preview_deploy_lock || exit 9          # must inherit the orchestrator's lock
kill_old() { pkill -f "fake_server.py .*--port 8768( |$)"; for _ in $(seq 50); do pgrep -f "fake_server.py .*--port 8768( |$)" >/dev/null || break; sleep 0.1; done; }
start() { (cd "$SIM_DIR" || exit 1; nohup "$SIM_PY" "$SIM_DIR/fake_server.py" --port 8768 "$@" >/dev/null 2>&1 </dev/null 9>&- &); sleep 0.8; }
echo "$sha" >>"$SIM_DIR/installs"            # counts installer invocations
case $SIM_INSTALL in
  slow_success)       kill_old; sleep 3; start --build-commit "$sha" --alerts --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 0 ;;
  fail_break_restore) kill_old; echo DEGRADED >"$SIM_DIR/old_health"; exit 1 ;;
  success)            kill_old; start --build-commit "$sha" --alerts --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 0 ;;
  fail_nochange)      exit 1 ;;
  fail_restored)      kill_old; start --build-commit "$SIM_OLD" --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 1 ;;
  fail_new_unhealthy) kill_old; echo DEGRADED >"$SIM_DIR/new_health"; start --build-commit "$sha" --alerts --health-file "$SIM_DIR/new_health" --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 1 ;;
  fail_port_empty)    kill_old; exit 1 ;;
  fail_unknown)       kill_old; start --build-commit 9999999999999999999999999999999999999999 --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 1 ;;
  fail_original_sick) echo DEGRADED >"$SIM_DIR/old_health"; exit 1 ;;
  verify_fails)       kill_old; start --build-commit "$sha" --alerts --opening-model-artifact "$SIM_DIR/other_artifact.json" --journal j.db; exit 0 ;;
esac
exit 1
'''

ARTIFACT = {"model_id": "b50b9dc30f6d664c", "decision_policy": {"threshold": 0.55},
            "model": {"kind": "opening_ensemble", "components": [
                {"preprocessor": {"names": [f"f{i}" for i in range(0, 87, 2)]}},
                {"preprocessor": {"names": [f"f{i}" for i in range(1, 87, 2)]}},
                {"preprocessor": {"names": [f"f{i}" for i in range(87)]}},
                {"kind": "volatility_baseline"}]}}
OLD = "1" * 40


def _ports_free():
    for port in PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return False
    return True


@pytest.fixture
def sim(tmp_path):
    if shutil.which("flock") is None or shutil.which("pgrep") is None:
        pytest.skip("flock/pgrep unavailable")
    if not _ports_free():
        pytest.skip("ports 8765/8767/8768 are in use on this machine")
    repo = tmp_path / "repo"
    (repo / "deploy/oracle").mkdir(parents=True)
    for name in ("deploy_preview_8768.sh", "preview_deploy_lock.sh", "deploy_job_8768.sh"):
        shutil.copy(ROOT / "deploy/oracle" / name, repo / "deploy/oracle" / name)
    (repo / "fake_installer.sh").write_text(FAKE_INSTALLER)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    simdir = tmp_path / "sim"
    (simdir / "bin").mkdir(parents=True)
    (simdir / "fake_server.py").write_text(FAKE_SERVER)
    (simdir / "bin/ss").write_text(FAKE_SS)
    (simdir / "bin/sudo").write_text("#!/bin/bash\nexit 1\n")
    for f in ("ss", "sudo"):
        (simdir / "bin" / f).chmod(0o755)
    (simdir / "artifact.json").write_text(json.dumps(ARTIFACT))
    (simdir / "other_artifact.json").write_text(json.dumps({**ARTIFACT, "model_id": "other"}))
    procs = []

    def start(port, build, *extra, env=None):
        p = subprocess.Popen([sys.executable, str(simdir / "fake_server.py"), "--port", str(port),
                              "--build-commit", build, *extra], cwd=simdir,
                             env=env or {"PATH": "/usr/bin:/bin"}, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        procs.append(p)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return p
            time.sleep(0.1)
        raise RuntimeError("fake server did not start")

    secret_env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/old/release", "SECRET_TOKEN": "s3cr3t-value-123",
                  "MULTI": "line one\nline two", "HOME": str(tmp_path)}
    old = start(8768, OLD, "--opening-model-artifact", str(simdir / "artifact.json"), "--journal", "j.db",
                "--health-file", str(simdir / "old_health"), env=secret_env)
    p65, p67 = start(8765, "prod"), start(8767, "p2")

    def make_env(scenario="success", extra_env=None):
        return {**os.environ, "PATH": f"{simdir / 'bin'}:/usr/bin:/bin", "PREVIEW_STATE_ROOT": str(tmp_path / "state"),
                "PREVIEW_PYTHON": sys.executable, "PREVIEW_TEST_INSTALLER": str(repo / "fake_installer.sh"),
                "PREVIEW_HEALTH_TIMEOUT_SECONDS": "15", "SIM_INSTALL": scenario, "SIM_DIR": str(simdir),
                "SIM_PY": sys.executable, "SIM_OLD": OLD, **(extra_env or {})}

    def run(*args, scenario="success", extra_env=None):
        env = make_env(scenario, extra_env)
        result = subprocess.run(["bash", str(repo / "deploy/oracle/deploy_preview_8768.sh"), *args],
                                env=env, capture_output=True, text=True, timeout=120)
        status = [l for l in result.stdout.splitlines() if l.startswith("DEPLOY_RESULT=")]
        return result, (status[-1].split("=", 1)[1] if status else None)

    def listener(port):
        out = subprocess.run(["pgrep", "-f", f"fake_server.py .*--port {port}( |$)"], capture_output=True, text=True)
        return out.stdout.split()[0] if out.stdout.split() else None

    def health():
        import urllib.request
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:8768/healthz", timeout=3) as r:
            return json.load(r)

    ctx = dict(run=run, make_env=make_env, repo=repo, sha=sha, listener=listener, health=health, old=old, p65=p65, p67=p67,
               simdir=simdir, state=tmp_path / "state", secret_env=secret_env, start=start)
    yield ctx
    subprocess.run(["pkill", "-f", str(simdir / "fake_server.py")])
    for p in procs:
        try:
            p.kill()
        except OSError:
            pass


def _environ(pid):
    return sorted(e for e in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if e)


def _unchanged_neighbours(sim):
    return sim["listener"](8765) == str(sim["p65"].pid) and sim["listener"](8767) == str(sim["p67"].pid)


def test_successful_install_verifies_artifact_threshold_and_neighbours(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    assert status == "SUCCESS" and result.returncode == 0, result.stdout + result.stderr
    assert sim["health"]()["build_commit"] == sim["sha"]
    assert _unchanged_neighbours(sim)


def test_failed_install_with_original_still_healthy_changes_nothing(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="fail_nochange")
    assert status == "FAILED_PREVIOUS_STILL_RUNNING" and result.returncode == 10
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_failed_install_that_installer_already_restored_is_confirmed_not_redone(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="fail_restored")
    assert status == "FAILED_PREVIOUS_STILL_RUNNING"
    assert sim["health"]()["build_commit"] == OLD


@pytest.mark.parametrize("scenario", ["fail_new_unhealthy", "fail_port_empty", "verify_fails"])
def test_unconfirmed_previous_build_triggers_guarded_restore_with_exact_environment(sim, scenario):
    original_env = _environ(sim["old"].pid)
    result, status = sim["run"]("deploy", sim["sha"], scenario=scenario)
    assert status == "ROLLED_BACK" and result.returncode == 11, result.stdout + result.stderr
    restored = sim["listener"](8768)
    assert restored and restored != str(sim["old"].pid)
    assert sim["health"]()["build_commit"] == OLD
    # The COMPLETE previous environment is restored exactly (secrets, multi-line values,
    # PYTHONPATH) and nothing from the deploying shell leaks in.
    assert _environ(restored) == original_env
    args = Path(f"/proc/{restored}/cmdline").read_bytes().split(b"\0")
    assert str(sim["simdir"] / "artifact.json").encode() in args and b"j.db" in args
    assert _unchanged_neighbours(sim)
    # Secrets are never printed.
    assert "s3cr3t-value-123" not in result.stdout + result.stderr


def test_unknown_process_on_8768_is_never_killed_and_manual_steps_are_printed(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="fail_unknown")
    assert status == "MANUAL_RECOVERY_REQUIRED" and result.returncode == 12
    assert sim["health"]()["build_commit"] == "9" * 40  # untouched
    assert "restore.sh" in result.stdout and "never touch ports 8765 or 8767" in result.stdout
    assert _unchanged_neighbours(sim)


def test_original_process_unhealthy_is_never_killed(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="fail_original_sick")
    assert status == "MANUAL_RECOVERY_REQUIRED"
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_concurrent_deployment_is_rejected_without_changes(sim):
    sim["state"].mkdir(mode=0o700, exist_ok=True)
    lock = sim["state"] / "deploy.lock"
    holder = subprocess.Popen(["flock", "-n", str(lock), "sleep", "10"])
    try:
        time.sleep(0.5)
        result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    finally:
        holder.kill()
    assert status == "BLOCKED_CONCURRENT_DEPLOYMENT" and result.returncode == 75
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_rollback_mode_refuses_wrong_identity_and_restores_this_runs_build(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    run_id = next(l.split("=", 1)[1] for l in result.stdout.splitlines() if l.startswith("DEPLOY_RUN_ID="))
    wrong, wrong_status = sim["run"]("rollback", "8" * 40, run_id)
    assert wrong_status == "MANUAL_RECOVERY_REQUIRED"  # 8768 is not build 888...: never killed
    assert sim["health"]()["build_commit"] == sim["sha"]
    ok, ok_status = sim["run"]("rollback", sim["sha"], run_id)
    assert ok_status == "ROLLED_BACK" and sim["health"]()["build_commit"] == OLD


def test_snapshot_files_are_private_and_hold_no_printed_secrets(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    run_id = next(l.split("=", 1)[1] for l in result.stdout.splitlines() if l.startswith("DEPLOY_RUN_ID="))
    run = sim["state"] / "runs" / run_id
    assert stat.S_IMODE(run.stat().st_mode) == 0o700
    for name in ("environ", "argv", "cwd", "pre.json"):
        assert stat.S_IMODE((run / name).stat().st_mode) == 0o600, name
    assert stat.S_IMODE((run / "restore.sh").stat().st_mode) == 0o700
    assert b"SECRET_TOKEN=s3cr3t-value-123" in (run / "environ").read_bytes()
    assert "s3cr3t" not in (run / "pre.json").read_text()


def test_switch_persists_full_environment_privately_for_manual_rollback(tmp_path):
    """write_rollback_command from the real switch script, isolated."""
    switch = (ROOT / "deploy/oracle/switch_preview_8768.sh").read_text()
    start = switch.index("write_rollback_command() {")
    end = switch.index("\nstart_old_in_memory() {")
    script = textwrap.dedent(f"""
        set -Eeuo pipefail
        STATE_ROOT={tmp_path}/state
        old_cwd={tmp_path}
        old_pythonpath={tmp_path}/legacy
        old_argv=(/usr/bin/env python -m x --journal j.db)
        old_env=(PATH=/usr/bin SECRET_TOKEN=abc "MULTI=a
b")
        {switch[start:end]}
        write_rollback_command
    """)
    subprocess.run(["bash", "-c", script], check=True)
    state = tmp_path / "state"
    entries = (state / "rollback.env").read_bytes().split(b"\0")
    assert b"SECRET_TOKEN=abc" in entries and b"MULTI=a\nb" in entries
    assert f"PYTHONPATH={tmp_path}/legacy".encode() in entries  # legacy code-identity fallback
    for name in ("rollback.env", "rollback.argv", "rollback.cwd"):
        assert stat.S_IMODE((state / name).stat().st_mode) == 0o600
    rollback = (state / "rollback.sh").read_text()
    assert 'env -i "${environment[@]}" "${argv[@]}"' in rollback and "SECRET_TOKEN" not in rollback


def _holds_lock(pid, lock):
    fds = Path(f"/proc/{pid}/fd")
    return any(os.path.realpath(f) == os.path.realpath(lock) for f in fds.iterdir() if f.is_symlink())


def test_running_dashboard_never_inherits_the_deployment_lock(sim):
    """Review defect: launched processes inherited fd 9 and held the lock
    forever, blocking every later deployment."""
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    assert status == "SUCCESS"
    new_pid = sim["listener"](8768)
    assert not _holds_lock(new_pid, sim["state"] / "deploy.lock")
    run_id = next(l.split("=", 1)[1] for l in result.stdout.splitlines() if l.startswith("DEPLOY_RUN_ID="))
    rolled, rolled_status = sim["run"]("rollback", sim["sha"], run_id)
    assert rolled_status == "ROLLED_BACK"  # not BLOCKED_CONCURRENT_DEPLOYMENT
    assert not _holds_lock(sim["listener"](8768), sim["state"] / "deploy.lock")


def test_orchestrator_returns_promptly_after_restoring_a_long_running_process(sim):
    """Review defect: a backgrounded `cd && nohup` list kept a bash subshell
    holding the caller's (SSH) pipes for the life of the restored preview."""
    started = time.time()
    result, status = sim["run"]("deploy", sim["sha"], scenario="fail_port_empty")
    assert status == "ROLLED_BACK" and time.time() - started < 60


def test_every_deploy_launch_detaches_stdin_and_closes_the_lock_fd():
    import re
    for name in ("switch_preview_8768.sh", "install_preview_8768.sh", "deploy_preview_8768.sh"):
        text = "\n".join(line for line in (ROOT / "deploy/oracle" / name).read_text().splitlines()
                         if not line.lstrip().startswith("#"))  # ignore explanatory comments
        # Each launch runs from `nohup` to the line that ends with the background `&`.
        launches = re.findall(r"nohup .*?&[ \t]*(?:\n|$)", text, flags=re.S)
        assert launches, name
        for launch in launches:
            assert "</dev/null" in launch and "9>&-" in launch, (name, launch[:80])
        assert not re.search(r"&&\s*nohup", text), name  # never background a `cd && nohup` list

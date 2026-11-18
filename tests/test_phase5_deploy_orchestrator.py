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
        soak = val("--soak", "good")
        if p == "/api/state":
            H.state_requests = getattr(H, "state_requests", 0) + 1
        if soak == "flip_unready" and os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "soak_started")):
            soak = "unready"
        # mid_slow: latency breaches only in the middle of the soak, then recovers,
        # so a check of only the final snapshot would pass.
        mid = soak == "mid_slow" and 4 <= getattr(H, "state_requests", 0) <= 5
        p95 = 5200 if soak == "slow" or mid else 2100
        samples = 10 if soak == "few_samples" else 300
        if p == "/healthz":
            H.health_requests = getattr(H, "health_requests", 0) + 1
        # late_*: misbehave only after post-install verification (inside the soak).
        if soak == "late_short_build" and getattr(H, "health_requests", 0) > 3:
            soak = "short_build"
        if soak == "late_state_error" and getattr(H, "state_requests", 0) > 3:
            soak = "state_error"
        body = {"/healthz": {"status": health, "build_commit": build[:7] if soak == "short_build" else build,
                             "signals_only": True, "workers": {"healthy": True}},
                "/readyz": {"status": "NOT_READY" if soak == "unready" else "READY", "signals_only": True,
                            "contract_quotes_fresh": soak != "unready", "reference_fresh": True,
                            "stale_reasons": ["LATEST_PUBLIC_POLL_FAILED"] if soak == "unready" else []},
                "/api/state": {"decision_intelligence": {"execution_capability": False},
                               "market_health": {"quote_age_seconds": 1.1, "reference_age_seconds": 0.7},
                               "runtime": {"state_version": 200 * getattr(H, "state_requests", 0),
                                           "latency_percentiles": {
                                   "publish_after_collection": {"p95_ms": p95, "p99_ms": 3300, "samples": samples},
                                   "prediction_calculation": {"p50_ms": 640, "samples": samples}}}}}.get(p)
        if p == "/readyz":
            H.ready_requests = getattr(H, "ready_requests", 0) + 1
        # stale_blip: one readiness probe stale by age only (6.5 s); stale_long: 12 s.
        if soak in ("stale_blip", "stale_long") and getattr(H, "ready_requests", 0) == 5:
            if p == "/readyz":
                body = {"status": "NOT_READY", "signals_only": True, "contract_quotes_fresh": False,
                        "reference_fresh": True, "stale_reasons": ["QUOTE_OLDER_THAN_5_SECONDS"]}
                data = json.dumps(body).encode(); self.send_response(503); self.end_headers(); self.wfile.write(data); return
            if p == "/api/state":
                body["market_health"] = {"quote_age_seconds": 6.5 if soak == "stale_blip" else 12.0,
                                         "reference_age_seconds": 0.8}
        if soak in ("rollover_flap", "reference_flap") and 3 <= getattr(H, "ready_requests", 0) <= 4 and p == "/readyz":
            body = {"status": "NOT_READY", "signals_only": True, "contract_quotes_fresh": False,
                    "reference_fresh": soak == "rollover_flap",
                    "stale_reasons": ["NO_ACTIVE_CONTRACT"] if soak == "rollover_flap" else ["REFERENCE_OLDER_THAN_5_SECONDS"]}
            data = json.dumps(body).encode(); self.send_response(503); self.end_headers(); self.wfile.write(data); return
        if p == "/api/state" and soak in ("rollover_flap", "reference_flap") and 3 <= getattr(H, "ready_requests", 0) <= 4:
            body["market_health"] = {"quote_age_seconds": None, "reference_age_seconds": 0.9}
        if p == "/api/state" and soak == "state_error":
            self.send_response(500); self.end_headers(); return
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
  soak_*)             kill_old; start --build-commit "$sha" --alerts --soak "${SIM_INSTALL#soak_}" --opening-model-artifact "$SIM_DIR/artifact.json" --journal j.db; exit 0 ;;
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
GATE_TEST_OVERRIDES = {"opening_window": False, "min_swap_free_mib": 0, "post_min_swap_free_mib": 0,
                       "cpu_sample_s": 0.3, "swap_sample_s": 0.2, "post_cpu_sample_s": 0.3,
                       "soak_seconds": 2, "soak_interval_s": 0.25, "post_max_load_increase": 100,
                       "max_load1": 100, "post_max_load1": 100,
                       # the sandbox clock is arbitrary: rollover tolerance off unless a test forces it
                       "rollover_before_s": 0, "rollover_after_s": 0}


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
    # A previous test's servers may still be exiting; never skip a safety test
    # for a transient port, only for a machine where the ports are really used.
    for _ in range(100):
        if _ports_free():
            break
        time.sleep(0.1)
    if not _ports_free():
        pytest.skip("ports 8765/8767/8768 are in use on this machine")
    repo = tmp_path / "repo"
    (repo / "deploy/oracle").mkdir(parents=True)
    for name in ("deploy_preview_8768.sh", "preview_deploy_lock.sh", "deploy_job_8768.sh", "preview_gates_8768.py"):
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
                "SIM_PY": sys.executable, "SIM_OLD": OLD,
                # Test-only gate settings (refused outside test mode): the
                # sandbox has no swap and an arbitrary clock; the soak is short.
                "PREVIEW_GATES_TEST_OVERRIDES": json.dumps(GATE_TEST_OVERRIDES),
                "PREVIEW_WATCH_INTERVAL": "1", "PREVIEW_WATCH_MAX_SECONDS": "180",
                **(extra_env or {})}

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
    # Wait until every fake service has exited and released its port, so the
    # next test does not see a transiently busy port and skip.
    for _ in range(100):
        if _ports_free() and subprocess.run(["pgrep", "-f", str(simdir / "fake_server.py")],
                                            capture_output=True).returncode != 0:
            break
        time.sleep(0.1)
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


# ---------------------------------------------------------------- gate tests
def _installs(sim):
    f = sim["simdir"] / "installs"
    return f.read_text().count("\n") if f.exists() else 0


@pytest.mark.parametrize("scenario,reason", [
    ("soak_unready", "/readyz not READY"),
    ("soak_slow", "publish p95"),
    ("soak_mid_slow", "publish p95"),
    ("soak_late_state_error", "/api/state unsuccessful"),
    ("soak_few_samples", "latency statistics were fully refreshed"),
    ("soak_rollover_flap", "outside the rollover window"),
    ("soak_late_short_build", "is not exactly"),
])
def test_soak_failure_rolls_back_to_the_exact_previous_process(sim, scenario, reason):
    old_env = _environ(sim["old"].pid)
    result, status = sim["run"]("deploy", sim["sha"], scenario=scenario)
    assert status == "ROLLED_BACK" and result.returncode == 11, result.stdout
    assert reason in result.stdout, result.stdout
    assert "soak failed" in result.stdout and "SUCCESS" not in result.stdout
    assert sim["health"]()["build_commit"] == OLD
    assert _environ(int(sim["listener"](8768))) == old_env      # exact previous environment
    assert _unchanged_neighbours(sim)


@pytest.mark.parametrize("scenario,reason", [("soak_state_error", "execution capability"),
                                             ("soak_short_build", "build is ")])
def test_broken_state_or_short_build_is_rejected_before_the_soak_too(sim, scenario, reason):
    result, status = sim["run"]("deploy", sim["sha"], scenario=scenario)
    assert status == "ROLLED_BACK" and reason in result.stdout, result.stdout
    assert sim["health"]()["build_commit"] == OLD and _unchanged_neighbours(sim)


def test_success_is_written_only_after_soak_and_postflight(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    assert status == "SUCCESS", result.stdout
    out = result.stdout
    assert out.index("preflight passed") < out.index("soak started") < out.index("soak passed") \
        < out.index("postflight passed") < out.index("DEPLOY_RESULT=SUCCESS")
    run = next((sim["state"] / "runs").iterdir())
    for f in ("preflight.json", "soak.json", "postflight.json"):
        assert oct((run / f).stat().st_mode & 0o777) == "0o600"
    assert json.loads((run / "soak.json").read_text())["result"] == "PASS"


def test_preflight_failure_blocks_before_any_change(sim):
    sim["p67"].kill(); sim["p67"].wait()                       # a protected listener is missing
    result, status = sim["run"]("deploy", sim["sha"], scenario="success")
    assert status == "BLOCKED" and "preflight: port 8767" in result.stdout
    assert _installs(sim) == 0 and sim["listener"](8768) == str(sim["old"].pid)


@pytest.mark.parametrize("override,text", [
    ({"min_mem_available_mib": 10**9}, "MemAvailable"),
    ({"max_load1": -1}, "load1"),
    ({"min_disk_free_mib": 10**12}, "disk free"),
    ({"min_swap_free_mib": 10**9}, "SwapFree"),
])
def test_preflight_resource_limits_block_without_change(sim, override, text):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps({**GATE_TEST_OVERRIDES, **override})})
    assert status == "BLOCKED" and text in result.stdout, result.stdout
    assert _installs(sim) == 0 and sim["listener"](8768) == str(sim["old"].pid)


@pytest.mark.parametrize("override,text", [
    ({"post_max_cpu_pct": -1}, "new 8768 CPU"),
    ({"post_min_mem_available_mib": 10**9}, "MemAvailable"),
])
def test_postflight_failure_rolls_back(sim, override, text):
    result, status = sim["run"]("deploy", sim["sha"], scenario="success",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps({**GATE_TEST_OVERRIDES, **override})})
    assert status == "ROLLED_BACK" and "postflight failed" in result.stdout and text in result.stdout, result.stdout
    assert sim["health"]()["build_commit"] == OLD and _unchanged_neighbours(sim)


def test_orchestrator_has_a_single_success_exit_after_the_gates():
    code = (ROOT / "deploy/oracle/deploy_preview_8768.sh").read_text()
    assert code.count("finish SUCCESS") == 1
    body = code[code.index("soak_and_finish() {"):code.index("\n}\n", code.index("soak_and_finish() {"))]
    assert body.index("soak") < body.index("postflight") < body.index("finish SUCCESS")
    assert 'soak_and_finish "$run" "$sha"' in code[code.index("if [[ $mode == recover ]]"):]


def _gates():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gates", ROOT / "deploy/oracle/preview_gates_8768.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opening_window_boundaries():
    g = _gates()
    base = 1794981600 - (1794981600 % 900)
    assert g.in_opening_window(base) and g.in_opening_window(base + 149)
    assert not g.in_opening_window(base + 150) and not g.in_opening_window(base + 809)
    assert g.in_opening_window(base + 810) and g.in_opening_window(base + 899)


def test_gate_overrides_are_refused_outside_test_mode(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PREVIEW_TEST_INSTALLER"}
    env["PREVIEW_GATES_TEST_OVERRIDES"] = json.dumps({"soak_seconds": 1})
    result = subprocess.run([sys.executable, str(ROOT / "deploy/oracle/preview_gates_8768.py"), "preflight",
                             str(tmp_path / "x.json")], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode == 1 and "only accepted in local test mode" in result.stdout


def test_production_limits_are_realistic_for_the_single_core_host():
    L = _gates().LIMITS
    assert L["soak_seconds"] == 900 and L["soak_interval_s"] == 10 and L["opening_window"] is True
    assert L["max_publish_p95_ms"] <= 3000 and L["max_publish_p99_ms"] <= 4500 and L["max_prediction_p50_ms"] <= 1020
    assert L["min_latency_samples"] >= 100 and L["min_evaluable_share"] >= 0.5 and L["max_age_s"] <= 10
    assert 8.4 < L["max_load1"] <= 12 and 512 <= L["min_mem_available_mib"] <= 2048
    assert L["post_max_cpu_increase_pts"] <= 10 and L["post_max_cpu_pct"] <= 40


def test_soak_requires_exact_40_character_build():
    result = subprocess.run([sys.executable, str(ROOT / "deploy/oracle/preview_gates_8768.py"), "soak",
                             "abc1234", "pre.json", "out.json"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 2 and "40-character" in result.stdout


ALWAYS_ROLLOVER = {**GATE_TEST_OVERRIDES, "rollover_before_s": 900, "rollover_after_s": 0}


def test_contract_rollover_not_ready_is_tolerated_only_inside_the_window(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="soak_rollover_flap",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps(ALWAYS_ROLLOVER)})
    assert status == "SUCCESS", result.stdout
    run = next((sim["state"] / "runs").iterdir())
    assert json.loads((run / "soak.json").read_text())["rollover_samples"] == 2


def test_stale_reference_is_never_tolerated_even_at_rollover(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="soak_reference_flap",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps(ALWAYS_ROLLOVER)})
    # A stale reference is never a tolerated rollover reason: it is scored as stale
    # (and here fails the 95% freshness rule) or fails immediately.
    assert status == "ROLLED_BACK" and "GATE FAIL: soak" in result.stdout and "rollover_samples" not in result.stdout, result.stdout
    assert sim["health"]()["build_commit"] == OLD


def test_latency_is_judged_only_on_a_window_refreshed_after_the_soak_began():
    g = _gates()
    src = (ROOT / "deploy/oracle/preview_gates_8768.py").read_text()
    assert 'version - version0 >= L["latency_window_cycles"]' in src and g.LIMITS["latency_window_cycles"] == 300


def test_rollover_window_boundaries():
    g = _gates()
    base = 1794981600 - (1794981600 % 900)
    assert g.in_rollover(g.LIMITS, base) and g.in_rollover(g.LIMITS, base + 59) and g.in_rollover(g.LIMITS, base - 20)
    assert not g.in_rollover(g.LIMITS, base + 60) and not g.in_rollover(g.LIMITS, base - 21)


def test_protected_identity_is_pid_and_start_time_only(tmp_path):
    g = _gates()
    ident = g.proc_identity(os.getpid())
    assert set(ident) == {"pid", "start"}           # run state (R/S/D) would flap
    assert g.proc_identity(os.getpid()) == ident


LONG_SOAK = {**GATE_TEST_OVERRIDES, "soak_seconds": 7, "soak_interval_s": 0.25}


def test_single_stale_blip_is_scored_by_the_95_percent_rule_not_fatal(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="soak_stale_blip",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps(LONG_SOAK)})
    assert status == "SUCCESS", result.stdout
    run = next((sim["state"] / "runs").iterdir())
    soak = json.loads((run / "soak.json").read_text())
    assert soak["stale_samples"] == 1 and soak["fresh_share"] >= 0.95


def test_stale_sample_above_ten_seconds_still_fails_immediately(sim):
    result, status = sim["run"]("deploy", sim["sha"], scenario="soak_stale_long",
                                extra_env={"PREVIEW_GATES_TEST_OVERRIDES": json.dumps(LONG_SOAK)})
    assert status == "ROLLED_BACK" and "outside 0..10.0" in result.stdout, result.stdout
    assert sim["health"]()["build_commit"] == OLD


def test_too_many_stale_samples_fail_the_95_percent_rule(sim):
    # default short soak: one stale sample out of ~8 is below 95% fresh
    result, status = sim["run"]("deploy", sim["sha"], scenario="soak_stale_blip")
    assert status == "ROLLED_BACK" and "1 stale" in result.stdout, result.stdout

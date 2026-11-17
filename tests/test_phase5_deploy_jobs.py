"""Phase 5 server-owned deployment jobs: SSH disconnect safety.

The "SSH session" is a process group that runs the job command exactly like
sshd would; a disconnect is simulated by killing that whole group (SIGHUP,
then SIGKILL) immediately after the old port-8768 process stops."""
import hashlib
import json
import os
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.test_phase5_deploy_orchestrator import OLD, _unchanged_neighbours, sim  # noqa: F401


def _job(sim, *args, scenario="success"):
    result = subprocess.run(["bash", str(sim["repo"] / "deploy/oracle/deploy_job_8768.sh"), *args],
                            env=sim["make_env"](scenario), capture_output=True, text=True, timeout=60)
    fields = {}
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith(("LOG:", "DEPLOY_JOB:")):
            key, _, value = line.partition("=")
            fields[key] = value
    return result, fields


def _wait_finished(sim, job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, fields = _job(sim, "status", job_id)
        if fields.get("DEPLOY_JOB_STATE") in {"FINISHED", "DIED"}:
            return fields
        time.sleep(1)
    raise AssertionError("job did not finish")


def _lock_released(sim):
    lock = sim["state"] / "deploy.lock"
    return subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0


def _wait_old_stopped(sim, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not Path(f"/proc/{sim['old'].pid}").exists() or sim["old"].poll() is not None:
            return
        time.sleep(0.05)
    raise AssertionError("old process never stopped")


def _ssh_session(sim, command, scenario):
    """A stand-in for the sshd session: its own process group running `command`."""
    return subprocess.Popen(["bash", "-c", command + "; sleep 600"], env=sim["make_env"](scenario),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _disconnect(session):
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(session.pid, sig)
        except ProcessLookupError:
            pass
        time.sleep(0.2)
    session.wait(timeout=10)


def _artifact_digest(sim):
    return hashlib.sha256((sim["simdir"] / "artifact.json").read_bytes()).hexdigest()


def _running_args(sim):
    return Path(f"/proc/{sim['listener'](8768)}/cmdline").read_bytes().split(b"\0")


def test_ssh_disconnect_right_after_old_process_stops_still_completes(sim):
    job_id = str(uuid.uuid4())
    digest = _artifact_digest(sim)
    session = _ssh_session(sim, f"bash {sim['repo']}/deploy/oracle/deploy_job_8768.sh start {sim['sha']} {job_id}",
                           "slow_success")
    _wait_old_stopped(sim)
    _disconnect(session)  # SSH drops while 8768 is offline
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_JOB_STATE"] == "FINISHED" and fields["DEPLOY_RESULT"] == "SUCCESS", fields
    health = sim["health"]()
    assert health["build_commit"] == sim["sha"] and health["status"] == "HEALTHY"
    assert _lock_released(sim)
    assert _unchanged_neighbours(sim)
    assert _artifact_digest(sim) == digest
    args = _running_args(sim)
    assert str(sim["simdir"] / "artifact.json").encode() in args and b"j.db" in args


def test_control_the_old_session_owned_orchestrator_dies_with_ssh(sim):
    """Proves the disconnect test detects the hazard: run the orchestrator
    directly in the session (the previous design) and disconnect."""
    session = _ssh_session(sim, f"bash {sim['repo']}/deploy/oracle/deploy_preview_8768.sh deploy {sim['sha']}",
                           "slow_success")
    _wait_old_stopped(sim)
    _disconnect(session)
    time.sleep(5)
    assert sim["listener"](8768) is None  # dashboard left offline: the blocker Codex reported


def test_interrupted_job_is_recovered_after_reconnect(sim):
    job_id = str(uuid.uuid4())
    result, fields = _job(sim, "start", sim["sha"], job_id, scenario="slow_success")
    assert fields["DEPLOY_JOB_ID"] == job_id
    _wait_old_stopped(sim)
    pid = int((sim["state"] / "jobs" / job_id / "pid").read_text())
    os.killpg(pid, signal.SIGKILL)  # the job itself dies (e.g. OOM kill) mid-deployment
    time.sleep(1)
    _, status = _job(sim, "status", job_id)
    assert status["DEPLOY_JOB_STATE"] == "DIED"
    assert _lock_released(sim)  # a dead holder cannot keep the lock
    _, recover = _job(sim, "recover", job_id, scenario="slow_success")
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "ROLLED_BACK", fields
    assert sim["health"]()["build_commit"] == OLD
    assert _lock_released(sim) and _unchanged_neighbours(sim)
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1  # recovery never reinstalls


def test_failed_rollback_reports_manual_recovery_and_releases_the_lock(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="fail_break_restore")
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "MANUAL_RECOVERY_REQUIRED"
    result, _ = _job(sim, "status", job_id)
    assert "LOG: DEPLOY_MANUAL" in result.stdout and "restore.sh" in result.stdout
    assert _lock_released(sim) and _unchanged_neighbours(sim)


def test_duplicate_and_concurrent_requests_never_start_a_second_installation(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="slow_success")
    again, fields = _job(sim, "start", sim["sha"], job_id, scenario="slow_success")
    assert fields["DEPLOY_JOB_ID"] == job_id and "no second installation" in again.stdout
    other, busy = _job(sim, "start", sim["sha"], str(uuid.uuid4()), scenario="slow_success")
    assert busy["DEPLOY_JOB_STATE"] == "BUSY" and busy["DEPLOY_ACTIVE_JOB"] == job_id
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    # After completion the same request still reports the finished job.
    _, done = _job(sim, "start", sim["sha"], job_id)
    assert done["DEPLOY_JOB_STATE"] == "FINISHED" and done["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1


def test_reconnecting_client_reads_status_without_relaunching(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    fields = _wait_finished(sim, job_id)  # a "new connection" polling by id only
    assert fields["DEPLOY_RESULT"] == "SUCCESS" and fields["DEPLOY_RUN_ID"]
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1


def test_job_directory_is_private_and_holds_no_secrets(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    _wait_finished(sim, job_id)
    job = sim["state"] / "jobs" / job_id
    assert stat.S_IMODE((sim["state"] / "jobs").stat().st_mode) == 0o700
    assert stat.S_IMODE(job.stat().st_mode) == 0o700
    for name in ("log", "result", "pid", "sha", "run_id"):
        assert stat.S_IMODE((job / name).stat().st_mode) == 0o600, name
        assert "s3cr3t-value-123" not in (job / name).read_text(), name


def test_invalid_requests_are_rejected_without_side_effects(sim):
    _, bad_sha = _job(sim, "start", "abc", str(uuid.uuid4()))
    _, bad_id = _job(sim, "start", sim["sha"], "x;rm -rf /")
    _, unknown = _job(sim, "status", str(uuid.uuid4()))
    assert bad_sha["DEPLOY_JOB_STATE"] == bad_id["DEPLOY_JOB_STATE"] == "REJECTED"
    assert unknown["DEPLOY_JOB_STATE"] == "UNKNOWN"
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_client_requested_rollback_runs_detached_and_only_after_success(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    _job(sim, "rollback", job_id)
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "ROLLED_BACK" and sim["health"]()["build_commit"] == OLD
    assert _lock_released(sim) and _unchanged_neighbours(sim)
    again, _ = _job(sim, "rollback", job_id)  # result is now ROLLED_BACK: refused
    assert "only a successful job" in again.stdout

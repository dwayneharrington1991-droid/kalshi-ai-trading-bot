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


def _job(sim, *args, scenario="success", extra_env=None):
    result = subprocess.run(["bash", str(sim["repo"] / "deploy/oracle/deploy_job_8768.sh"), *args],
                            env=sim["make_env"](scenario, extra_env), capture_output=True, text=True, timeout=60)
    fields = {}
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith(("LOG:", "DEPLOY_JOB:")):
            key, _, value = line.partition("=")
            fields[key] = value
    return result, fields


def _wait_finished(sim, job_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, fields = _job(sim, "status", job_id, sim["sha"])
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
    # Watchdog effectively idle here, so the client-requested recovery path is tested.
    result, fields = _job(sim, "start", sim["sha"], job_id, scenario="slow_success",
                          extra_env={"PREVIEW_WATCH_INTERVAL": "600"})
    assert fields["DEPLOY_JOB_ID"] == job_id
    _wait_old_stopped(sim)
    pid = int((sim["state"] / "jobs" / job_id / "pid").read_text())
    os.killpg(pid, signal.SIGKILL)  # the job itself dies (e.g. OOM kill) mid-deployment
    time.sleep(1)
    _, status = _job(sim, "status", job_id, sim["sha"])
    assert status["DEPLOY_JOB_STATE"] == "DIED"
    assert _lock_released(sim)  # a dead holder cannot keep the lock
    _, recover = _job(sim, "recover", job_id, sim["sha"], scenario="slow_success")
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "ROLLED_BACK", fields
    assert sim["health"]()["build_commit"] == OLD
    assert _lock_released(sim) and _unchanged_neighbours(sim)
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1  # recovery never reinstalls


def test_watchdog_recovers_a_dead_orchestrator_without_any_client(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="slow_success")
    _wait_old_stopped(sim)
    pid = int((sim["state"] / "jobs" / job_id / "pid").read_text())
    os.killpg(pid, signal.SIGKILL)          # orchestrator dies; nobody is connected
    deadline, fields = time.time() + 120, {}
    while time.time() < deadline:            # status reads only; no recover request is sent
        _, fields = _job(sim, "status", job_id, sim["sha"])
        if fields.get("DEPLOY_JOB_STATE") == "FINISHED":
            break
        time.sleep(1)
    assert fields["DEPLOY_RESULT"] in {"ROLLED_BACK", "SUCCESS"}, fields
    log = (sim["state"] / "jobs" / job_id / "log").read_text()
    assert "watchdog: orchestrator gone without a result; requesting guarded recovery" in log
    if fields["DEPLOY_RESULT"] == "SUCCESS":  # only possible after a fresh full soak
        assert "soak passed" in log and "postflight passed" in log
    else:
        assert sim["health"]()["build_commit"] == OLD
    assert _lock_released(sim) and _unchanged_neighbours(sim)


def test_failed_rollback_reports_manual_recovery_and_releases_the_lock(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="fail_break_restore")
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "MANUAL_RECOVERY_REQUIRED"
    result, _ = _job(sim, "status", job_id, sim["sha"])
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
    _, unknown = _job(sim, "status", str(uuid.uuid4()), sim["sha"])
    assert bad_sha["DEPLOY_JOB_STATE"] == bad_id["DEPLOY_JOB_STATE"] == "REJECTED"
    assert unknown["DEPLOY_JOB_STATE"] == "UNKNOWN"
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_client_requested_rollback_runs_detached_and_only_after_success(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    _job(sim, "rollback", job_id, sim["sha"])
    fields = _wait_finished(sim, job_id)
    assert fields["DEPLOY_RESULT"] == "ROLLED_BACK" and sim["health"]()["build_commit"] == OLD
    assert _lock_released(sim) and _unchanged_neighbours(sim)
    again, _ = _job(sim, "rollback", job_id, sim["sha"])  # result is now ROLLED_BACK: refused
    assert "only a successful job" in again.stdout


# ---------------------------------------------------------------- blockers


@pytest.mark.parametrize("bad_id", [
    "12345678-AAAA-bbbb-cccc-1234567890ab",       # uppercase: not canonical
    "12345678-aaaa-bbbb-cccc-1234567890ab;id",    # injection attempt
    "$(touch /tmp/pwned)", "../../etc", "", "a" * 36,
])
def test_job_ids_must_be_canonical_lowercase_uuids(sim, bad_id):
    for command in (["start", sim["sha"], bad_id], ["status", bad_id, sim["sha"]],
                    ["recover", bad_id, sim["sha"]], ["rollback", bad_id, sim["sha"]]):
        result, fields = _job(sim, *command)
        assert result.returncode == 2 and fields["DEPLOY_JOB_STATE"] == "REJECTED", (command, result.stdout)
    assert not (sim["simdir"] / "installs").exists()
    assert not Path("/tmp/pwned").exists()
    assert sim["listener"](8768) == str(sim["old"].pid)


def test_every_job_is_bound_to_its_exact_commit(sim):
    job_id = str(uuid.uuid4())
    other = "f" * 40
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    for command in (["status", job_id, other], ["recover", job_id, other], ["rollback", job_id, other],
                    ["start", other, job_id]):
        result, fields = _job(sim, *command)
        assert fields["DEPLOY_JOB_STATE"] == "SHA_MISMATCH" and result.returncode == 2, command
    # Nothing was rolled back or reinstalled by the mismatched requests.
    assert sim["health"]()["build_commit"] == sim["sha"]
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    assert (sim["state"] / "jobs" / job_id / "sha").read_text().strip() == sim["sha"]


def _parallel(sim, commands, scenario):
    procs = [subprocess.Popen(["bash", str(sim["repo"] / "deploy/oracle/deploy_job_8768.sh"), *c],
                              env=sim["make_env"](scenario), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for c in commands]
    return [p.communicate(timeout=90)[0] for p in procs]


def test_simultaneous_identical_requests_create_exactly_one_job(sim):
    job_id = str(uuid.uuid4())
    outputs = _parallel(sim, [["start", sim["sha"], job_id]] * 8, "slow_success")
    assert all(f"DEPLOY_JOB_ID={job_id}" in out for out in outputs)
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    jobs = [p for p in (sim["state"] / "jobs").iterdir()]
    assert [p.name for p in jobs] == [job_id]  # no duplicate or half-written (.new-*) directories
    assert (sim["state"] / "jobs" / job_id / "sha").read_text().strip() == sim["sha"]


def test_simultaneous_different_requests_start_only_one_deployment(sim):
    ids = [str(uuid.uuid4()) for _ in range(8)]
    outputs = _parallel(sim, [["start", sim["sha"], i] for i in ids], "slow_success")
    started = [i for i, out in zip(ids, outputs) if "DEPLOY_JOB_STATE=BUSY" not in out]
    assert len(started) == 1, outputs
    assert all("DEPLOY_JOB_STATE=BUSY" in out for i, out in zip(ids, outputs) if i not in started)
    assert _wait_finished(sim, started[0])["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    assert sorted(p.name for p in (sim["state"] / "jobs").iterdir()) == started
    assert _lock_released(sim) and _unchanged_neighbours(sim)


def test_job_files_are_private_after_atomic_creation(sim):
    job_id = str(uuid.uuid4())
    _job(sim, "start", sim["sha"], job_id, scenario="success")
    _wait_finished(sim, job_id)
    job = sim["state"] / "jobs" / job_id
    assert stat.S_IMODE(job.stat().st_mode) == 0o700
    for name in ("sha", "created_at", "log", "pid", "result"):
        assert stat.S_IMODE((job / name).stat().st_mode) == 0o600, name
    assert stat.S_IMODE((sim["state"] / "jobs.lock").stat().st_mode) == 0o600

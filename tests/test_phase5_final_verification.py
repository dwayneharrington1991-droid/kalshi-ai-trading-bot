"""Adversarial Phase 5 deployment verification owned by Codex.

These tests intentionally remain separate from Claude's implementation tests.
They exercise externally visible job semantics and inspect the Windows client
only where this Linux environment cannot execute Windows PowerShell 5.1.
"""
import subprocess
import time
import uuid
from pathlib import Path

from tests.test_phase5_deploy_jobs import _job, _wait_finished
from tests.test_phase5_deploy_orchestrator import _unchanged_neighbours, sim  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def test_existing_request_id_is_rejected_for_a_different_commit(sim):
    """Idempotency must never alias one request id to a different SHA."""
    job_id = str(uuid.uuid4())
    _, started = _job(sim, "start", sim["sha"], job_id, scenario="slow_success")
    assert started["DEPLOY_JOB_ID"] == job_id

    other_sha = "a" * 40
    assert other_sha != sim["sha"]
    result, fields = _job(sim, "start", other_sha, job_id, scenario="slow_success")

    assert fields.get("DEPLOY_JOB_STATE") in {"REJECTED", "SHA_MISMATCH"}, result.stdout
    assert (sim["state"] / "jobs" / job_id / "sha").read_text().strip() == sim["sha"]
    assert _wait_finished(sim, job_id)["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    assert _unchanged_neighbours(sim)


def test_simultaneous_job_creation_has_one_atomic_winner(sim):
    """Different simultaneous request ids must not all create job records."""
    script = sim["repo"] / "deploy/oracle/deploy_job_8768.sh"
    requests = [str(uuid.uuid4()) for _ in range(12)]
    env = sim["make_env"]("slow_success")
    processes = [
        subprocess.Popen(
            ["bash", str(script), "start", sim["sha"], request],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for request in requests
    ]
    outputs = [process.communicate(timeout=30)[0] for process in processes]

    created = [path for path in (sim["state"] / "jobs").iterdir() if path.is_dir()]
    assert len(created) == 1, outputs
    winner = created[0].name
    assert _wait_finished(sim, winner)["DEPLOY_RESULT"] == "SUCCESS"
    assert (sim["simdir"] / "installs").read_text().count("\n") == 1
    assert _unchanged_neighbours(sim)


def test_windows_client_validates_resume_id_before_remote_interpolation():
    """ResumeJobId is user input and must not become unquoted Bash syntax."""
    client = (ROOT / "deploy/windows/Deploy-Preview8768.ps1").read_text()
    pattern = client.find("$UuidPattern = '^[0-9a-f]{8}-")
    validation = client.find("$ResumeJobId -cnotmatch $UuidPattern")
    first_ssh = client.find("Invoke-Remote 'echo SSH_PREFLIGHT_OK'")

    assert pattern >= 0, "PowerShell client has no canonical lowercase UUID pattern"
    assert validation >= 0, "PowerShell client has no strict ResumeJobId validation"
    assert first_ssh >= 0 and pattern < validation < first_ssh


def test_job_status_sha_is_compared_with_requested_sha_by_windows_client():
    """A resumed job cannot be reported as the requested build by assumption."""
    client = (ROOT / "deploy/windows/Deploy-Preview8768.ps1").read_text()
    assert "DEPLOY_JOB_SHA" in client
    assert "[string]::Equals($jobSha, $Sha, [System.StringComparison]::Ordinal)" in client
    assert "Assert-JobSha $f 'start'" in client
    assert "Assert-JobSha $f 'status'" in client
    assert "Assert-JobSha $f 'rollback status'" in client


def test_disconnect_and_identity_regressions_remain_in_the_suite():
    jobs = (ROOT / "tests/test_phase5_deploy_jobs.py").read_text()
    orchestrator = (ROOT / "tests/test_phase5_deploy_orchestrator.py").read_text()
    assert "test_ssh_disconnect_right_after_old_process_stops_still_completes" in jobs
    assert "test_interrupted_job_is_recovered_after_reconnect" in jobs
    assert "test_successful_install_verifies_artifact_threshold_and_neighbours" in orchestrator
    assert '"threshold": 0.55' in orchestrator
    assert "range(87)" in orchestrator

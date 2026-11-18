"""Static guards for the Windows client (PowerShell cannot run on Linux CI).

The behavioural tests are deploy/windows/Test-Deploy-Preview8768.ps1 (run on
Windows PowerShell 5.1). These checks keep the DEPLOY_JOB_SHA verification
from being silently removed and keep both scripts structurally balanced."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "deploy/windows/Deploy-Preview8768.ps1"
HARNESS = ROOT / "deploy/windows/Test-Deploy-Preview8768.ps1"


def _scan(text):
    """Quote-aware bracket balance: '' and backtick escapes, here-strings, comments."""
    depth, close = {"{": 0, "(": 0, "[": 0}, {"}": "{", ")": "(", "]": "["}
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("@'\n", i) or text.startswith('@"\n', i):
            i = text.find("\n" + text[i + 1] + "@", i + 2) + 3
            continue
        if c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            i += 1
            while i < n and not (text[i] == "'" and text[i + 1:i + 2] != "'"):
                i += 2 if text[i:i + 2] == "''" else 1
            i += 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "`" else 1
            i += 1
            continue
        if c in depth:
            depth[c] += 1
        elif c in close:
            depth[close[c]] -= 1
            assert depth[close[c]] >= 0, f"unbalanced {c} near offset {i}"
        i += 1
    return depth


def test_client_verifies_deploy_job_sha_on_every_start_and_status_response():
    text = CLIENT.read_text()
    assert "[string]::Equals($jobSha, $Sha, [System.StringComparison]::Ordinal)" in text  # case-sensitive
    assert "did not include DEPLOY_JOB_SHA" in text
    # Start: verified before the job is accepted.
    assert re.search(r"-ceq \$JobId\) \{ Assert-JobSha \$f 'start'; \$started = \$f \}", text)
    # Status: verified before any state is acted on (success, recovery, rollback).
    status_poll = text.index("$state = $f['DEPLOY_JOB_STATE']")
    switch = text.index("switch ($state)", status_poll)
    assert "Assert-JobSha $f 'status'" in text[status_poll:switch]
    rollback_poll = text.index("function Invoke-ClientRollback")
    assert "Assert-JobSha $f 'rollback status'" in text[rollback_poll:]
    assert "$JobStates = @('STARTING', 'RUNNING', 'DIED', 'FINISHED')" in text


def test_harness_covers_matching_missing_and_mismatched_job_shas():
    text = HARNESS.read_text()
    for name in ("matching DEPLOY_JOB_SHA on start and status succeeds",
                 "DEPLOY_JOB_SHA on start is BLOCKED before polling",
                 "DEPLOY_JOB_SHA on a SUCCESS status is BLOCKED (no success, no rollback)",
                 "mismatched DEPLOY_JOB_SHA on a DIED status never triggers recovery",
                 "missing DEPLOY_JOB_SHA on a RUNNING status stops polling",
                 "mismatched DEPLOY_JOB_SHA on a rollback status is BLOCKED"):
        assert name in text, name
    assert "@{ Name = 'missing'" in text and "@{ Name = 'mismatched'" in text and "@{ Name = 'different case'" in text


def test_both_powershell_scripts_are_structurally_balanced():
    for path in (CLIENT, HARNESS):
        assert _scan(path.read_text()) == {"{": 0, "(": 0, "[": 0}, path.name
        assert all(ord(ch) < 128 for ch in path.read_text()), path.name

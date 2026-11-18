"""The failed-job evidence collector must stay strictly read-only."""
import base64
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/collect_failed_job_evidence.sh"
DOC = ROOT / "docs/PHASE5_FAILED_JOB_EVIDENCE.md"
JOB = "01997b30-95d3-4642-be05-5a0b48cd691d"
SHA = "ae05d37301790388f2cf985413ec5849fde970a1"


def _code_lines():
    return [line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]


def test_script_never_changes_processes_journal_lock_or_reads_environment():
    code = "\n".join(_code_lines())
    for forbidden in (r"\bkill\b", r"\bflock\b", r"\bsqlite3?\b", r"/environ", r"\bsystemctl\b",
                      r"\brm\b", r"\bmv\b", r"\bnohup\b", r"\bsetsid\b", r"deploy_job_8768\.sh",
                      r"switch_preview", r"install_preview", r"\benv\b\s*$", r"\bprintenv\b"):
        assert not re.search(forbidden, code, re.M), forbidden
    # The job snapshot is listed (names only), never read.
    assert not re.search(r"(cat|<)\s*\"?\$run/", code)


def test_doc_embeds_the_script_verbatim_and_survives_windows_line_endings():
    doc = DOC.read_text()
    embedded = re.search(r"\$Script = @'\n(.*?)\n'@", doc, re.S).group(1)
    assert embedded == SCRIPT.read_text().rstrip()
    sent = embedded.replace("\n", "\r\n").replace("\r\n", "\n")
    assert base64.b64decode(base64.b64encode(sent.encode())).decode() == embedded
    assert f"$JobId = '{JOB}'" in doc and f"$Sha = '{SHA}'" in doc


def _run(tmp_path, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "PREVIEW_STATE_ROOT": str(tmp_path),
             "PREVIEW_LOG_DIR": str(tmp_path / "logs"), "PREVIEW_PYTHON": "python3"},
    )


def test_rejects_malformed_job_id_and_commit(tmp_path):
    for args in (("../../etc", SHA), (JOB.upper(), SHA), (JOB, SHA.upper()), (JOB, "abc"), ()):
        result = _run(tmp_path, *args)
        assert result.returncode == 2 and "COLLECTED_AT" not in result.stdout


def test_reports_job_log_and_server_log_with_credentials_redacted(tmp_path):
    job = tmp_path / "jobs" / JOB
    job.mkdir(parents=True)
    (job / "sha").write_text(SHA + "\n")
    (job / "run_id").write_text("r1\n")
    (job / "result").write_text("DEPLOY_RESULT=FAILED_PREVIOUS_STILL_RUNNING\n")
    (job / "log").write_text("PREVIEW_READINESS_PROGRESS elapsed=30s\nKALSHI_API_KEY=supersecret\n")
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    (tmp_path / "runs" / "r1" / "env.nul").write_text("HIDDEN_VALUE=shouldnotappear\0")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / f"preview-8768-{SHA[:7]}.log").write_text(
        'Traceback (most recent call last):\nValueError: boom\nauthorization: Bearer xyz\n'
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = _run(tmp_path, JOB, SHA)
    out = result.stdout
    assert "commit binding: MATCH" in out and "FAILED_PREVIOUS_STILL_RUNNING" in out
    assert "PREVIEW_READINESS_PROGRESS elapsed=30s" in out and "ValueError: boom" in out
    assert "env.nul" in out
    for secret in ("supersecret", "shouldnotappear", "Bearer xyz"):
        assert secret not in out
    assert "EVIDENCE_COLLECTION_COMPLETE" in out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

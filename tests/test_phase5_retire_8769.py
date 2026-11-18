"""Approval-gated 8769 retirement script: static guarantees and a real local
stop/rollback rehearsal (test ports, temporary journal; never Oracle)."""
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/retire_service_8769.sh"
CODE = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))


def test_shell_syntax_and_shellcheck():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout


def test_never_force_kills_deletes_or_rewrites_journals():
    for forbidden in (r"kill\s+-9", r"SIGKILL", r"kill\s+-KILL", r"\brm\b", r"\bmv\b", r"\btruncate\b",
                      r"VACUUM", r"sqlite3\s", r"DELETE\s+FROM", r"UPDATE\s", r"systemctl\s+(stop|start|restart|kill)",
                      r"--account-readonly\s*\\?\s*$"):
        assert not re.search(forbidden, CODE), forbidden
    assert "pidfd_open" in CODE and "pidfd_send_signal" in CODE
    assert "NOT force-killed" in CODE
    # Environment is captured privately and never echoed.
    assert 'cp "/proc/$TARGET_PID/environ" "$S/env"' in CODE and "chmod 600" in CODE
    assert not re.search(r"(cat|echo|printf)[^\n]*\$S/env", CODE)


def test_retire_and_rollback_require_explicit_approval_and_locks():
    assert 'retire --approve <token from check>' in CODE
    assert '[[ $3 == "$(token)" ]]' in CODE
    assert '${4:-} == ROLLBACK-8769' in CODE
    assert "flock -n 8" in CODE and "flock -n 7" in CODE


def test_usage_errors_block_without_changes(tmp_path):
    for args in ([], ["retire"], ["retire", "--approve"], ["rollback", str(tmp_path)], ["bogus"]):
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "RETIRE_PYTHON": sys.executable,
                                     "RETIRE_STATE_ROOT": str(tmp_path / "state")})
        assert result.returncode == 2


# ---------------------------------------------------------------- rehearsal
needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("ss", "fuser", "git", "setsid")) or not hasattr(os, "pidfd_open"),
    reason="rehearsal needs ss, fuser, git, setsid and pidfd support",
)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listen(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise AssertionError(f"port {port} never listened")


def _listener(port):
    out = subprocess.run(["ss", "-Hltnp", f"sport = :{port}"], capture_output=True, text=True).stdout
    found = re.findall(r"pid=(\d+)", out)
    return int(found[0]) if found else None


@pytest.fixture
def rehearsal(tmp_path):
    """A real signals server playing '8769' in a git checkout at a known commit,
    plus two protected stand-ins; all on free local ports."""
    stage = tmp_path / "stage"
    subprocess.run(["git", "worktree", "add", "--detach", str(stage), "HEAD"], cwd=ROOT,
                   check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(stage), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    (stage / "logs").mkdir()
    journal = stage / "logs" / "repair-test-rehearsal.db"
    port, p1, p2 = _free_port(), _free_port(), _free_port()
    procs = []

    def launch(cwd, args):
        # setsid: the server is orphaned like a nohup process on Oracle.
        procs.append(subprocess.Popen(["setsid", sys.executable, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True))

    launch(stage, ["-m", "src.signals_display.server", "--mode", "public", "--host", "127.0.0.1",
                   "--port", str(port), "--journal", str(journal), "--build-commit", commit[:7],
                   "--disable-scheduled-learning"])
    launch(tmp_path, ["-m", "http.server", str(p1), "--bind", "127.0.0.1"])
    launch(ROOT, ["-m", "src.signals_display.server", "--mode", "public", "--host", "127.0.0.1",
                  "--port", str(p2), "--journal", str(tmp_path / "protected.db"), "--build-commit", "prot",
                  "--disable-scheduled-learning"])
    for p in (port, p1, p2):
        _wait_listen(p)
    time.sleep(3)  # journal lock taken, workers started
    home = tmp_path / "home"
    home.mkdir()
    # The sandbox has no cron; a stand-in reports an empty user crontab exactly
    # like `crontab -l` does on Oracle (otherwise the script fails closed).
    stub = tmp_path / "stubbin"
    stub.mkdir()
    if not shutil.which("crontab"):
        (stub / "crontab").write_text('#!/bin/sh\necho "no crontab for $(id -un)" >&2\nexit 1\n')
        (stub / "crontab").chmod(0o755)
    env = {
        **os.environ,
        "RETIRE_PORT": str(port), "RETIRE_EXPECT_PID": str(_listener(port)),
        "RETIRE_EXPECT_CWD": str(stage), "RETIRE_EXPECT_COMMIT": commit,
        "RETIRE_JOURNAL_NAME": journal.name, "RETIRE_PROTECTED_PORTS": f"{p1} {p2}",
        "RETIRE_HEALTH_PROBE_PORTS": str(p2), "RETIRE_STATE_ROOT": str(tmp_path / "state"),
        "RETIRE_DEPLOY_LOCK": str(tmp_path / "deploy.lock"), "RETIRE_PYTHON": sys.executable,
        "RETIRE_CLIENT_SAMPLES": "2", "RETIRE_CLIENT_INTERVAL": "1", "RETIRE_READY_TIMEOUT": "90",
        # Rehearsal-only: the sandbox has no market feed (readiness cannot pass),
        # and orphans are adopted by the sandbox's own init process.
        "RETIRE_REQUIRE_READY": "0", "RETIRE_ALLOWED_PARENTS": "systemd init process_api tini",
        "RETIRE_SEARCH_HOME": str(home),
        "PATH": f"{stub}:{os.environ['PATH']}",
    }
    (tmp_path / "deploy.lock").touch()
    try:
        yield {"env": env, "port": port, "protected": (p1, p2), "journal": journal, "home": home,
               "stage": stage, "tmp": tmp_path}
    finally:
        for p in (port, p1, p2):
            pid = _listener(p)
            if pid:
                os.kill(pid, 15)
        for proc in procs:
            proc.terminate()
        time.sleep(1)
        subprocess.run(["git", "worktree", "remove", "--force", str(stage)], cwd=ROOT, capture_output=True)


def _run(env, *args, timeout=240):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout)


@needs_tools
def test_rehearsal_check_retire_and_verified_rollback(rehearsal):
    env, port = rehearsal["env"], rehearsal["port"]
    protected_before = [_listener(p) for p in rehearsal["protected"]]
    check = _run(env, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    token = re.search(r"APPROVAL TOKEN: (\S+)", check.stdout).group(1)
    assert token.startswith("RETIRE-8769-PID")

    original = _listener(port)
    wrong = _run(env, "retire", "--approve", "RETIRE-8769-PID1-START1")
    assert wrong.returncode == 2 and _listener(port) == original

    retire = _run(env, "retire", "--approve", token)
    assert retire.returncode == 0, retire.stdout + retire.stderr
    assert "SIGTERM_SENT" in retire.stdout and "RETIRED" in retire.stdout
    assert "protected services unchanged" in retire.stdout
    assert _listener(port) is None
    assert rehearsal["journal"].exists() and rehearsal["journal"].stat().st_size > 0
    assert [_listener(p) for p in rehearsal["protected"]] == protected_before
    state = Path(re.search(r"rollback (\S+) --approve", retire.stdout).group(1))
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    assert all(oct(f.stat().st_mode & 0o777) == "0o600" for f in state.iterdir())
    assert "HOME=" not in retire.stdout and "PATH=" not in retire.stdout   # env never printed

    refused = _run(env, "rollback", str(state), "--approve", "yes")
    assert refused.returncode == 2 and _listener(port) is None

    rollback = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    for line in ("argv identical, 14 arguments", "verify: commit", "signals-only", "protected services unchanged",
                 "ROLLBACK VERIFIED", "readiness NOT required: rehearsal override"):
        assert line in rollback.stdout, line
    restored = _listener(port)
    assert restored and restored != original
    assert [_listener(p) for p in rehearsal["protected"]] == protected_before

    again = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert again.returncode == 2 and "already in use" in again.stdout


@needs_tools
def test_rehearsal_fails_closed_on_live_usage_and_dependencies(rehearsal):
    env, port = rehearsal["env"], rehearsal["port"]
    original = _listener(port)

    client = socket.create_connection(("127.0.0.1", port))              # a live client
    try:
        result = _run(env, "check")
        assert result.returncode == 2 and "clients:" in result.stdout
    finally:
        client.close()
    time.sleep(1)

    reader = open(rehearsal["journal"], "rb")                           # another journal reader
    try:
        result = _run(env, "check")
        assert result.returncode == 2 and "other processes hold it open" in result.stdout
    finally:
        reader.close()

    (rehearsal["home"] / "bin").mkdir()                                 # a restart script
    (rehearsal["home"] / "bin" / "keepalive.sh").write_text(f"while true; do start --port {port}; done\n")
    result = _run(env, "check")
    assert result.returncode == 2 and "supervisors: references" in result.stdout
    (rehearsal["home"] / "bin" / "keepalive.sh").unlink()

    broken = {**env, "RETIRE_PROTECTED_PORTS": f"{rehearsal['protected'][0]} 1"}   # protected missing
    result = _run(broken, "check")
    assert result.returncode == 2 and "protected services" in result.stdout

    wrong_commit = {**env, "RETIRE_EXPECT_COMMIT": "0000000"}
    result = _run(wrong_commit, "retire", "--approve", "anything")
    assert result.returncode == 2

    no_tool = {**env, "PATH": "/nonexistent"}                           # cannot verify -> blocked
    result = subprocess.run(["/bin/bash", str(SCRIPT), "check"], capture_output=True, text=True,
                            env=no_tool, timeout=60)
    assert result.returncode == 2 and "required tool missing" in result.stdout

    assert _listener(port) == original                                  # nothing was ever stopped

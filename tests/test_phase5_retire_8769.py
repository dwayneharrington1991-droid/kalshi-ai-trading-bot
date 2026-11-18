"""Approval-gated 8769 retirement script: static guarantees and real local
stop/rollback rehearsals (test ports, temporary journals; never Oracle)."""
import fcntl
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


def _block(start, end):
    return CODE[CODE.index(start):CODE.index(end, CODE.index(start))]


# ------------------------------------------------------------------ static
def test_shell_syntax_and_shellcheck():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    if shutil.which("shellcheck"):
        result = subprocess.run(["shellcheck", "-S", "warning", str(SCRIPT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout


def test_never_force_kills_deletes_or_writes_journals():
    for forbidden in (r"kill\s+-9", r"SIGKILL", r"kill\s+-KILL", r"(^|[^a-z_])rm\s", r"\bmv\b", r"\btruncate\b",
                      r"VACUUM", r"sqlite3\s", r"DELETE\s+FROM", r"systemctl\s+(--\w+\s+)?(stop|start|restart|kill)"):
        assert not re.search(forbidden, CODE, re.M), forbidden
    assert "pidfd_open" in CODE and "pidfd_send_signal" in CODE and "NOT force-killed" in CODE
    assert not re.search(r"(cat|echo|printf)[^\n]*\$S/env", CODE)


def test_defect1_zombie_is_treated_as_exited_then_listener_and_journal_release_verified():
    assert "${s%% *} != Z" in CODE
    retire = _block("  retire)", "  rollback)")
    assert "alive_same" in retire and "journal handles released" in retire and "port $PORT free" in retire


def test_defect2_every_capture_is_checked_and_validated_before_sigterm():
    retire = _block("  retire)", "pidfd_term \"$TARGET_PID\"")
    for item in ("argv", "environment", "cwd", "journal", "commit", "port", "identity", "journal files",
                 "protected snapshot", "permissions"):
        assert f'die "capture failed: {item}"' in retire, item
    assert 'validate_state "$S" || die' in retire
    validator = _block("validate_state() {", "\n}\n")
    for f in ("argv", "env", "cwd", "journal", "commit", "port", "identity", "journal-files", "protected-before"):
        assert f in validator
    assert '== 600' in validator and '== 700' in validator


def test_defect3_no_silent_failure_paths_in_checks():
    sup = _block("gate_supervisors() {", "\n}\n")
    assert "|| true" not in sup.replace("{ grep -vE '^[[:space:]]*#' <<<\"$out\" || true; }", "")
    assert "2>/dev/null; echo" not in CODE
    assert 'systemctl "$scope" list-units' in sup and 'list-timers' in sup and "failed\"; return" in sup
    refs = _block("gate_process_refs() {", "\n}\n")
    assert "cannot read command lines" in refs
    holders = _block("journal_holders() {", "\n}\n")
    assert "(( rc == 0 || rc == 1 )) || return 1" in holders


def test_defect4_deploy_lock_always_created_secured_and_acquired():
    locks = _block("acquire_locks() {", "\n}\n")
    assert 'secure_lock 7 "$DEPLOY_LOCK"' in locks and "if [[ -e $DEPLOY_LOCK" not in CODE
    secure = _block("secure_lock() {", "\n}\n")
    assert "symlink" in secure and "chmod 600" in secure and "flock -n" in secure and "not owned" in secure


def test_defect5_supervisor_checks_repeated_immediately_before_sigterm():
    final = _block("final re-check immediately before SIGTERM", "pidfd_term \"$TARGET_PID\"")
    for gate in ("gate_supervisors", "gate_journal_users", "gate_process_refs", "gate_parent", "gate_clients 1",
                 "protected_snapshot", "healthy"):
        assert gate in final, gate


def test_defect6_8768_health_is_enforced_after_retirement():
    post = _block("process exited", "RETIRED: PID")
    assert 'if healthy "$p"' in post and "POST FAIL: $p not healthy/signals-only" in post and "rc=4" in post
    health = _block("healthy() {", "\n}\n")
    assert '"HEALTHY"' in health and '"signals_only") is True' in health and '"healthy") is True' in health


def test_defect7_rollback_compares_against_saved_snapshot():
    rollback = _block("  rollback)", "  *)")
    assert 'SAVED=$(<"$S/protected-before")' in rollback
    assert rollback.count('== "$SAVED"') >= 2          # before relaunch and after verification


def test_usage_errors_block_without_changes(tmp_path):
    for args in ([], ["retire"], ["retire", "--approve"], ["retire", "--approve", "x", "extra"],
                 ["rollback", str(tmp_path)], ["bogus"]):
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "RETIRE_PYTHON": sys.executable,
                                     "RETIRE_STATE_ROOT": str(tmp_path / "state"),
                                     "RETIRE_DEPLOY_LOCK": str(tmp_path / "d" / "deploy.lock")})
        assert result.returncode == 2


# --------------------------------------------------------------- rehearsal
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


def _make_rehearsal(tmp_path, *, zombie=False):
    stage = tmp_path / "stage"
    subprocess.run(["git", "worktree", "add", "--detach", str(stage), "HEAD"], cwd=ROOT,
                   check=True, capture_output=True)
    commit = subprocess.run(["git", "-C", str(stage), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    (stage / "logs").mkdir()
    journal = stage / "logs" / "repair-test-rehearsal.db"
    port, p1, p2 = _free_port(), _free_port(), _free_port()
    procs = []

    def launch(cwd, args, orphan=True):
        prefix = ["setsid"] if orphan else []          # orphan: adopted by init, like nohup on Oracle
        procs.append(subprocess.Popen([*prefix, sys.executable, *args], cwd=cwd, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True))

    # zombie=True: this test process stays the parent and does not reap, so
    # the stopped service remains a zombie (Codex defect 1).
    launch(stage, ["-m", "src.signals_display.server", "--mode", "public", "--host", "127.0.0.1",
                   "--port", str(port), "--journal", str(journal), "--build-commit", commit[:7],
                   "--disable-scheduled-learning"], orphan=not zombie)
    launch(tmp_path, ["-m", "http.server", str(p1), "--bind", "127.0.0.1"])
    launch(ROOT, ["-m", "src.signals_display.server", "--mode", "public", "--host", "127.0.0.1",
                  "--port", str(p2), "--journal", str(tmp_path / "protected.db"), "--build-commit", "prot",
                  "--disable-scheduled-learning"])
    for p in (port, p1, p2):
        _wait_listen(p)
    time.sleep(3)
    home = tmp_path / "home"
    home.mkdir()
    # The sandbox has no cron and no running systemd; stand-ins report what an
    # unused account shows on Oracle (otherwise the script fails closed).
    stub = tmp_path / "stubbin"
    stub.mkdir()
    if not shutil.which("crontab"):
        (stub / "crontab").write_text('#!/bin/sh\necho "no crontab for $(id -un)" >&2\nexit 1\n')
    if subprocess.run(["systemctl", "--system", "list-units"], capture_output=True).returncode != 0 \
            if shutil.which("systemctl") else True:
        (stub / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    for f in stub.iterdir():
        f.chmod(0o755)
    own_comm = Path("/proc/self/comm").read_text().strip()
    env = {
        **os.environ,
        "RETIRE_PORT": str(port), "RETIRE_EXPECT_PID": str(_listener(port)),
        "RETIRE_EXPECT_CWD": str(stage), "RETIRE_EXPECT_COMMIT": commit,
        "RETIRE_JOURNAL_NAME": journal.name, "RETIRE_PROTECTED_PORTS": f"{p1} {p2}",
        "RETIRE_HEALTH_PROBE_PORTS": str(p2), "RETIRE_STATE_ROOT": str(tmp_path / "state"),
        "RETIRE_DEPLOY_LOCK": str(tmp_path / "deploylock" / "deploy.lock"), "RETIRE_PYTHON": sys.executable,
        "RETIRE_CLIENT_SAMPLES": "2", "RETIRE_CLIENT_INTERVAL": "1", "RETIRE_READY_TIMEOUT": "90",
        # Rehearsal-only: no market feed in the sandbox (readiness cannot pass);
        # orphans are adopted by the sandbox init; in the zombie rehearsal the
        # test process itself is the parent.
        "RETIRE_REQUIRE_READY": "0",
        "RETIRE_ALLOWED_PARENTS": "systemd init process_api tini" + (f" {own_comm}" if zombie else ""),
        "RETIRE_SEARCH_HOME": str(home),
        "PATH": f"{stub}:{os.environ['PATH']}",
    }
    info = {"env": env, "port": port, "protected": [p1, p2], "journal": journal, "home": home,
            "stage": stage, "tmp": tmp_path, "procs": procs}

    def cleanup():
        for p in (port, p1, p2):
            pid = _listener(p)
            if pid:
                os.kill(pid, 15)
        for proc in procs:
            proc.terminate()
        time.sleep(1)
        for proc in procs:
            proc.poll()
        subprocess.run(["git", "worktree", "remove", "--force", str(stage)], cwd=ROOT, capture_output=True)
    return info, cleanup


@pytest.fixture
def rehearsal(tmp_path):
    info, cleanup = _make_rehearsal(tmp_path)
    try:
        yield info
    finally:
        cleanup()


@pytest.fixture
def zombie_rehearsal(tmp_path):
    info, cleanup = _make_rehearsal(tmp_path, zombie=True)
    try:
        yield info
    finally:
        cleanup()


def _run(env, *args, timeout=300):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=timeout)


def _token(env):
    check = _run(env, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    return re.search(r"APPROVAL TOKEN: (\S+)", check.stdout).group(1)


@needs_tools
def test_rehearsal_check_retire_and_verified_rollback(rehearsal):
    env, port = rehearsal["env"], rehearsal["port"]
    protected_before = [_listener(p) for p in rehearsal["protected"]]
    token = _token(env)
    assert token.startswith("RETIRE-8769-PID")
    original = _listener(port)

    wrong = _run(env, "retire", "--approve", "RETIRE-8769-PID1-START1")
    assert wrong.returncode == 2 and _listener(port) == original

    retire = _run(env, "retire", "--approve", token)
    assert retire.returncode == 0, retire.stdout + retire.stderr
    for line in ("locks held", "launch state captured and validated", "final re-check immediately before SIGTERM",
                 "gate ok: supervisors", "SIGTERM_SENT", "post: port", "post: journal handles released",
                 "post: protected services unchanged", "HEALTHY, workers healthy, signals-only", "RETIRED"):
        assert line in retire.stdout, line
    lock = Path(env["RETIRE_DEPLOY_LOCK"])
    assert lock.exists() and oct(lock.stat().st_mode & 0o777) == "0o600"     # created and secured
    assert _listener(port) is None
    assert rehearsal["journal"].exists() and rehearsal["journal"].stat().st_size > 0
    assert [_listener(p) for p in rehearsal["protected"]] == protected_before
    state = Path(re.search(r"rollback (\S+) --approve", retire.stdout).group(1))
    assert oct(state.stat().st_mode & 0o777) == "0o700"
    assert all(oct(f.stat().st_mode & 0o777) == "0o600" for f in state.iterdir())
    assert "HOME=" not in retire.stdout and "PATH=" not in retire.stdout

    assert _run(env, "rollback", str(state), "--approve", "yes").returncode == 2 and _listener(port) is None

    rollback = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert rollback.returncode == 0, rollback.stdout + rollback.stderr
    for line in ("pre-rollback: protected services match the saved pre-retirement snapshot",
                 "argv identical, 14 arguments", "verify: commit", "signals-only (no account flags)",
                 "journal held only by restored PID", "HEALTHY, workers healthy, signals-only",
                 "verify: protected services match the saved pre-retirement snapshot",
                 "readiness NOT required: rehearsal override", "ROLLBACK VERIFIED"):
        assert line in rollback.stdout, line
    restored = _listener(port)
    assert restored and restored != original
    assert [_listener(p) for p in rehearsal["protected"]] == protected_before

    again = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert again.returncode == 2 and "already in use" in again.stdout


@needs_tools
def test_rehearsal_zombie_counts_as_exited(zombie_rehearsal):
    env, port = zombie_rehearsal["env"], zombie_rehearsal["port"]
    token = _token(env)
    retire = _run(env, "retire", "--approve", token)
    assert retire.returncode == 0, retire.stdout + retire.stderr
    assert "zombie awaiting its parent: treated as exited" in retire.stdout
    assert "post: port" in retire.stdout and "post: journal handles released" in retire.stdout
    assert _listener(port) is None


@needs_tools
def test_rehearsal_rollback_refuses_when_protected_differ_from_saved_snapshot(rehearsal):
    env, port = rehearsal["env"], rehearsal["port"]
    retire = _run(env, "retire", "--approve", _token(env))
    assert retire.returncode == 0, retire.stdout
    state = Path(re.search(r"rollback (\S+) --approve", retire.stdout).group(1))
    p1 = rehearsal["protected"][0]                       # protected stand-in is replaced
    os.kill(_listener(p1), 15)
    time.sleep(1)
    rehearsal["procs"].append(subprocess.Popen(["setsid", sys.executable, "-m", "http.server", str(p1), "--bind",
                                                "127.0.0.1"], cwd=rehearsal["tmp"], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, start_new_session=True))
    _wait_listen(p1)
    result = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert result.returncode == 2 and "differ from the saved pre-retirement snapshot" in result.stdout
    assert _listener(port) is None


@needs_tools
def test_rehearsal_rollback_rejects_tampered_state(rehearsal):
    env = rehearsal["env"]
    retire = _run(env, "retire", "--approve", _token(env))
    assert retire.returncode == 0, retire.stdout
    state = Path(re.search(r"rollback (\S+) --approve", retire.stdout).group(1))
    (state / "commit").chmod(0o644)
    result = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert result.returncode == 2 and "not 0600" in result.stdout
    (state / "commit").chmod(0o600)
    (state / "commit").write_text("not-a-commit\n")
    result = _run(env, "rollback", str(state), "--approve", "ROLLBACK-8769")
    assert result.returncode == 2 and "bad commit" in result.stdout
    assert _listener(rehearsal["port"]) is None


@needs_tools
def test_rehearsal_fails_closed_on_usage_dependencies_and_unverifiable_checks(rehearsal):
    env, port, home = rehearsal["env"], rehearsal["port"], rehearsal["home"]
    original = _listener(port)

    def blocked(result, text):
        assert result.returncode == 2 and text in result.stdout, result.stdout

    client = socket.create_connection(("127.0.0.1", port))
    try:
        blocked(_run(env, "check"), "clients:")
    finally:
        client.close()
    time.sleep(1)

    with open(rehearsal["journal"], "rb"):
        blocked(_run(env, "check"), "other processes hold it open")

    (home / "bin").mkdir()
    (home / "bin" / "keepalive.sh").write_text(f"while true; do start --port {port}; done\n")
    blocked(_run(env, "check"), "supervisors: references")
    (home / "bin" / "keepalive.sh").unlink()

    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").symlink_to(home / "missing-target")       # unreadable entry
    blocked(_run(env, "check"), "could not fully search")
    (home / ".ssh" / "config").unlink()

    failing = Path(env["PATH"].split(":")[0]) / "systemctl"
    original_stub = failing.read_text() if failing.exists() else None
    failing.write_text("#!/bin/sh\nexit 1\n"); failing.chmod(0o755)
    blocked(_run(env, "check"), "systemctl --system list-units failed")
    if original_stub is None:
        failing.unlink()
    else:
        failing.write_text(original_stub)

    blocked(_run({**env, "RETIRE_HEALTH_PROBE_PORTS": str(rehearsal["protected"][0])}, "check"),
            "is not HEALTHY with healthy workers and signals-only")
    blocked(_run({**env, "RETIRE_PROTECTED_PORTS": f"{rehearsal['protected'][0]} 1"}, "check"),
            "protected services are not each exactly one readable listener")
    assert _run({**env, "RETIRE_EXPECT_COMMIT": "0000000"}, "retire", "--approve", "anything").returncode == 2

    lock = Path(env["RETIRE_DEPLOY_LOCK"])                                # deployment running
    lock.parent.mkdir(mode=0o700, exist_ok=True)
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        blocked(_run(env, "retire", "--approve", _token(env)), "is held")
    lock.unlink()
    lock.symlink_to(rehearsal["tmp"] / "elsewhere")                        # symlinked lock
    blocked(_run(env, "retire", "--approve", _token(env)), "is a symlink")
    lock.unlink()

    result = subprocess.run(["/bin/bash", str(SCRIPT), "check"], capture_output=True, text=True,
                            env={**env, "PATH": "/nonexistent"}, timeout=60)
    blocked(result, "required tool missing")

    assert _listener(port) == original                                    # nothing was ever stopped

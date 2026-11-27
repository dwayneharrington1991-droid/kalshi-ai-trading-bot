"""Rehearsal of the approved 8765/8767 retirement on stand-in processes (high ports)."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/retire_old_bots.sh"
OLD_A, OLD_B, KEEP = 28765, 28767, 28768

KEEP_SERVER = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/healthz": {"status": "HEALTHY", "signals_only": True, "workers": {"healthy": True}},
                "/readyz": {"status": "READY", "signals_only": True}}.get(self.path.split("?")[0], {})
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''
OLD_SERVER = ("import socket,time,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
              "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); time.sleep(120)")


def wait_port(port):
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


@pytest.fixture
def stage(tmp_path):
    procs = []
    (tmp_path / "keep.db").write_bytes(b"")
    (tmp_path / "old_a.db").write_bytes(b"")
    (tmp_path / "old_b.db").write_bytes(b"")
    keep = subprocess.Popen([sys.executable, "-c", KEEP_SERVER, str(KEEP), "--journal", str(tmp_path / "keep.db")],
                            start_new_session=True)
    procs.append(keep)
    a = subprocess.Popen([sys.executable, "-c", OLD_SERVER, str(OLD_A), "--journal", str(tmp_path / "old_a.db")],
                         start_new_session=True, env={**os.environ, "SECRET_TOKEN": "never-print-me"})
    b = subprocess.Popen([sys.executable, "-c", OLD_SERVER, str(OLD_B), "--journal", str(tmp_path / "old_b.db")],
                         start_new_session=True)
    procs += [a, b]
    for port in (KEEP, OLD_A, OLD_B):
        wait_port(port)
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    cron = tmp_path / "crontab.txt"
    cron.write_text(f"@reboot python serve.py --port {OLD_A} --journal old_a.db\n@reboot python serve.py --port {KEEP}\n"
                    f"*/5 * * * * echo keep\n")
    (bin_dir / "crontab").write_text(f'#!/bin/bash\nif [[ $1 == -l ]]; then cat "{cron}"; else cat >"{cron}"; fi\n')
    (bin_dir / "crontab").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RETIRE_OLD_PORTS": f"{OLD_A} {OLD_B}",
           "RETIRE_KEEP_PORT": str(KEEP), "RETIRE_STATE_ROOT": str(tmp_path / "state"),
           "RETIRE_DEPLOY_LOCK": str(tmp_path / "deploy.lock"), "RETIRE_PYTHON": sys.executable,
           "RETIRE_RESPAWN_WATCH": "1", "RETIRE_CPU_SAMPLE": "1", "RETIRE_STOP_WAIT": "10"}
    yield dict(tmp=tmp_path, env=env, keep=keep, a=a, b=b, cron=cron)
    for p in procs:
        p.kill()


def test_retires_both_old_bots_keeps_8768_and_saves_rollback_bundles(stage):
    run = subprocess.run(["bash", str(SCRIPT), "run"], env=stage["env"], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "RETIRE_RESULT: RETIRED" in run.stdout and "nothing deleted" in run.stdout
    assert stage["a"].wait(5) is not None and stage["b"].wait(5) is not None   # both stopped (one SIGTERM)
    assert stage["keep"].poll() is None                                         # kept service untouched
    assert "never-print-me" not in run.stdout + run.stderr
    cron = stage["cron"].read_text()
    assert f"--port {OLD_A}" not in cron and f"--port {KEEP}" in cron and "echo keep" in cron
    bundles = list((stage["tmp"] / "state").glob(f"*/{OLD_A}"))
    assert len(bundles) == 1
    b = bundles[0]
    assert (b / "argv.nul").exists() and "never-print-me" in (b / "environ.nul").read_text(errors="replace")
    assert oct(b.stat().st_mode & 0o777) == "0o700" or oct((b / "environ.nul").stat().st_mode & 0o777) == "0o600"
    assert subprocess.run(["sha256sum", "-c", "--quiet", "MANIFEST.sha256"], cwd=b).returncode == 0
    assert "CPU_BEFORE:" in run.stdout and "CPU_AFTER:" in run.stdout
    for f in ("old_a.db", "old_b.db", "keep.db"):
        assert (stage["tmp"] / f).exists()                                      # nothing deleted


def test_rollback_relaunches_a_retired_manual_service(stage):
    assert subprocess.run(["bash", str(SCRIPT), "run"], env=stage["env"], capture_output=True, timeout=120).returncode == 0
    b = next((stage["tmp"] / "state").glob(f"*/{OLD_B}"))
    rb = subprocess.run(["bash", str(SCRIPT), "rollback", str(b)], env=stage["env"], capture_output=True, text=True, timeout=60)
    assert rb.returncode == 0, rb.stdout + rb.stderr
    wait_port(OLD_B)
    subprocess.run(["fuser", "-k", f"{OLD_B}/tcp"], capture_output=True)


def test_refuses_when_8768_is_not_healthy(stage):
    stage["keep"].kill(); stage["keep"].wait()
    env = {**stage["env"]}
    run = subprocess.run(["bash", str(SCRIPT), "run"], env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 2 and "BLOCKED" in run.stdout
    assert stage["a"].poll() is None and stage["b"].poll() is None             # nothing stopped


def test_refuses_a_looping_launcher(stage, tmp_path):
    stage["b"].kill(); stage["b"].wait()
    loop = subprocess.Popen(["bash", "-c", f"while true; do {sys.executable} -c \"{OLD_SERVER}\" {OLD_B}; sleep 1; done"],
                            start_new_session=True)
    try:
        wait_port(OLD_B)
        run = subprocess.run(["bash", str(SCRIPT), "run"], env=stage["env"], capture_output=True, text=True, timeout=120)
        assert run.returncode == 2 and "loop that would restart it" in run.stdout, run.stdout
    finally:
        os.killpg(loop.pid, 9)


def test_script_never_deletes_and_never_names_8768_for_stopping():
    code = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    import re
    assert not re.search(r"\brm\b|\bshred\b|\btruncate\b|VACUUM|kill -9|SIGKILL", code)
    assert "pidfd_send_signal(fd, signal.SIGTERM)" in code

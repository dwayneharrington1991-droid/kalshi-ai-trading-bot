"""Post-deployment live verification refuses anything but the exact, healthy, fresh build."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/verify_8768_live.sh"
PORT = 28868
SERVER = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
build, age = sys.argv[2], float(sys.argv[3])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = {"/healthz": {"status": "HEALTHY", "signals_only": True, "build_commit": build, "workers": {"healthy": True}},
             "/readyz": {"status": "READY" if age <= 5 else "NOT_READY", "signals_only": True},
             "/api/state": {"market_health": {"quote_age_seconds": age, "reference_age_seconds": 0.5},
                            "runtime": {"latency_percentiles": {}}}}.get(self.path.split("?")[0], {})
        d = json.dumps(b).encode(); self.send_response(200); self.end_headers(); self.wfile.write(d)
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''
SHA = "b" * 40


@pytest.fixture
def live(tmp_path):
    procs = []
    def start(build=SHA, age=0.8):
        (tmp_path / "live.db").write_bytes(b"")
        p = subprocess.Popen([sys.executable, "-c", SERVER, str(PORT), build, str(age), "--journal", str(tmp_path / "live.db")])
        procs.append(p)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", PORT)) == 0:
                    return
            time.sleep(0.1)
    cfg = tmp_path / "alerts.env"
    def run(*extra):
        env = {**os.environ, "VERIFY_PYTHON": sys.executable, "VERIFY_PORT": str(PORT), "VERIFY_OLD_PORTS": "28865 28867",
               "VERIFY_RETRY_SLEEP": "0", "VERIFY_ALERTS_FILE": str(cfg)}
        return subprocess.run(["bash", str(SCRIPT), SHA, *extra], env=env, capture_output=True, text=True, timeout=60)
    yield dict(start=start, run=run, cfg=cfg)
    for p in procs:
        p.kill(); p.wait()


def test_verified_only_for_exact_build_healthy_ready_fresh_and_telegram(live):
    live["start"]()
    live["cfg"].write_text("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=5\n"); live["cfg"].chmod(0o600)
    r = live["run"]("--require-telegram")
    assert r.returncode == 0 and "LIVE_8768: VERIFIED" in r.stdout and "telegram: CONFIGURED" in r.stdout
    assert "1:x" not in r.stdout and "TELEGRAM_CHAT_ID=5" not in r.stdout


def test_rolled_back_old_build_is_never_reported_done(live):
    live["start"](build="c" * 40)
    r = live["run"]()
    assert r.returncode == 1 and "NOT VERIFIED (failed: exact build)" in r.stdout


def test_stale_data_fails(live):
    live["start"](age=12)
    r = live["run"]()
    assert r.returncode == 1 and "fresh data" in r.stdout and "READY" in r.stdout


def test_missing_or_shared_telegram_file_fails_when_required(live):
    live["start"]()
    assert "telegram configured" in live["run"]("--require-telegram").stdout
    live["cfg"].write_text("TELEGRAM_BOT_TOKEN=1:x\nTELEGRAM_CHAT_ID=5\n"); live["cfg"].chmod(0o644)
    r = live["run"]("--require-telegram")
    assert r.returncode == 1 and "telegram configured" in r.stdout


def test_not_running_fails(live):
    r = live["run"]()
    assert r.returncode == 1 and "LIVE_8768: NOT RUNNING" in r.stdout


def test_deploy_client_fails_on_rollback_and_on_any_non_success():
    client = (ROOT / "deploy/windows/Deploy-Preview8768.ps1").read_text()
    assert "'ROLLED_BACK' { throw" in client                       # rollback -> nonzero exit
    assert "default { throw \"DEPLOY RESULT UNKNOWN" in client and "'MANUAL_RECOVERY_REQUIRED' { throw" in client

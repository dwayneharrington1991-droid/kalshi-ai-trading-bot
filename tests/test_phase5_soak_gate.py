"""Post-deployment soak gate for preview 8768: strict thresholds, read-only."""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/oracle/verify_preview_8768_soak.sh"
SHA = "f64f088736288c84fca3d53809de73e349d15a6c"


def _server(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code, body = state[self.path]
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _good(**over):
    runtime = {"processing_ms": 400, "latency_percentiles": {
        "publish_after_collection": {"p95_ms": 2100, "p99_ms": 3200},
        "prediction_calculation": {"p50_ms": 600}}}
    runtime.update(over.pop("runtime", {}))
    state = {
        "/healthz": (200, {"status": "HEALTHY", "signals_only": True, "workers": {"healthy": True},
                           "build_commit": SHA[:7]}),
        "/readyz": (200, {"status": "READY", "signals_only": True, "contract_quotes_fresh": True,
                          "reference_fresh": True}),
        "/api/state?view=main": (200, {"market_health": {"quote_age_seconds": 1.2, "reference_age_seconds": 0.8},
                                        "runtime": runtime}),
    }
    state.update(over)
    return state


def _run(port, *extra, protected_port=None):
    env = {**os.environ, "SOAK_PORT": str(port), "SOAK_PYTHON": sys.executable, "SOAK_INTERVAL": "0.2",
           "SOAK_TEST_SECONDS": "1.2", "SOAK_MIN_MINUTES": "5"}
    if protected_port:
        env["SOAK_PROTECTED_PORT"] = str(protected_port)
    return subprocess.run(["bash", str(SCRIPT), SHA, "5", *extra], capture_output=True, text=True, env=env, timeout=60)


needs_ss = pytest.mark.skipif(not shutil.which("ss"), reason="needs ss")


def test_syntax_and_read_only():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    code = "\n".join(l for l in SCRIPT.read_text().splitlines() if not l.lstrip().startswith("#"))
    for forbidden in (r"\bkill\b", r"systemctl", r"deploy_job_8768", r"\brm\b", r"method=.POST", r"urlopen\(.*data="):
        assert not re.search(forbidden, code), forbidden
    assert "127.0.0.1:{port}" in code          # only the preview port is contacted over HTTP


@needs_ss
def test_pass_when_all_thresholds_hold():
    srv = _server(_good())
    try:
        result = _run(srv.server_address[1])
    finally:
        srv.shutdown()
    assert result.returncode == 0 and "SOAK PASS" in result.stdout, result.stdout


@needs_ss
@pytest.mark.parametrize("override,finding", [
    ({"runtime": {"latency_percentiles": {"publish_after_collection": {"p95_ms": 4253, "p99_ms": 5153},
                                          "prediction_calculation": {"p50_ms": 600}}}}, "publish-after-collection p95"),
    ({"runtime": {"latency_percentiles": {"publish_after_collection": {"p95_ms": 2000, "p99_ms": 3000},
                                          "prediction_calculation": {"p50_ms": 1698}}}}, "prediction-calculation p50"),
    ({"/api/state?view=main": (200, {"market_health": {"quote_age_seconds": 6.5, "reference_age_seconds": 0.8},
                                      "runtime": {}})}, "fresh (<=5 s) in only"),
    ({"/readyz": (503, {"status": "NOT_READY"})}, "readyz not READY"),
    ({"/healthz": (200, {"status": "HEALTHY", "signals_only": True, "workers": {"healthy": True},
                         "build_commit": "6c19623"})}, "expected build"),
    ({"/healthz": (200, {"status": "HEALTHY", "signals_only": False, "workers": {"healthy": True},
                         "build_commit": SHA[:7]})}, "signals-only"),
])
def test_fail_on_each_threshold(override, finding):
    srv = _server(_good(**override))
    try:
        result = _run(srv.server_address[1])
    finally:
        srv.shutdown()
    assert result.returncode == 4 and "SOAK FAIL" in result.stdout and finding in result.stdout, result.stdout


@needs_ss
def test_fail_when_protected_listener_differs():
    srv = _server(_good())
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0)); other.listen()
        try:
            result = _run(srv.server_address[1], "999999", protected_port=other.getsockname()[1])
        finally:
            srv.shutdown()
    assert result.returncode == 4 and "protected" in result.stdout


def test_usage_errors():
    for args in ([], ["zz", "15"], [SHA, "2"], [SHA, "45"], [SHA, "15", "abc"]):
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                                env={**os.environ, "SOAK_PYTHON": sys.executable})
        assert result.returncode == 2

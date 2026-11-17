"""Preview 8768 readiness regressions (failed f7d1672 deployment, HTTP 503).

Optional per-tick data capture must never make the core prediction state
stale, and the guarded switch must explain which readiness endpoint failed
instead of dying on an unexplained HTTPError.
"""
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.signals_display.monitor import Journal
from src.signals_display.server import Monitor
from tests.test_signals_monitor import frame

ROOT = Path(__file__).resolve().parents[1]
SWITCH = ROOT / "deploy" / "oracle" / "switch_preview_8768.sh"


class Feed:
    def poll(self):
        return frame()


def test_optional_capture_failures_never_turn_ticks_into_market_data_failures(tmp_path, monkeypatch):
    monitor = Monitor(Feed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    baseline = monitor.snapshot()
    assert not baseline.get("connection_error")

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected optional capture failure")

    monkeypatch.setattr(monitor.journal, "record_opening_quote", boom)
    monkeypatch.setattr(monitor, "remember_preopening_flow", boom)
    errors = []
    original = monitor.runtime_meter.error
    monkeypatch.setattr(monitor.runtime_meter, "error",
                        lambda component, name: (errors.append(component), original(component, name)))
    monitor.tick()
    state = monitor.snapshot()
    assert not state.get("connection_error"), state.get("warnings")
    assert "prediction" not in errors
    assert {"opening_quote_capture", "preopening_flow_capture"} <= set(errors)


def test_malformed_order_flow_coverage_is_ignored():
    class Holder:
        preopening_flow = {}
    market = {"ticker": "KXBTC15M-Q", "opens_at": "2026-11-01T12:00:00+00:00",
              "closes_at": "2026-11-01T12:15:00+00:00"}
    from datetime import datetime, timezone
    now = datetime(2026, 11, 1, 11, 59, 30, tzinfo=timezone.utc)
    for coverage in (None, {"Kraken public order flow": None},
                     {"Kraken public order flow": {"data": {"spot": "not-a-dict", "spot_book": None}}}):
        Monitor.remember_preopening_flow(Holder(), {"markets": [market], "coverage": coverage}, now)
        assert Holder.preopening_flow == {}


class _Server(BaseHTTPRequestHandler):
    responses = {}

    def log_message(self, *_args):
        pass

    def do_GET(self):
        code, body = self.responses.get(self.path, (404, {}))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


def _probe_source():
    source = SWITCH.read_text()
    start = source.index("readiness_report() {")
    return source[start: source.index("\n}\n", start) + 3]


def test_switch_readiness_probe_names_the_failing_endpoint_and_reason():
    _Server.responses = {
        "/healthz": (200, {"status": "HEALTHY", "build_commit": "abc", "signals_only": True,
                           "workers": {"healthy": True}}),
        "/readyz": (503, {"status": "NOT_READY", "signals_only": True, "contract_quotes_fresh": False,
                          "reference_fresh": True}),
        "/api/candle-integrity": (200, {"status": "READY", "bars": 1440, "unique_bars": 1440}),
        "/api/state?view=main": (200, {"decision_intelligence": {"execution_capability": False}}),
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = _probe_source() + f'\nPYTHON={sys.executable}\nPREVIEW_PROBE_PORT={server.server_address[1]}\nreadiness_report abc\n'
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
    assert result.returncode != 0
    assert "/readyz HTTP 503" in result.stdout
    assert '"contract_quotes_fresh": false' in result.stdout
    assert "Traceback" not in result.stderr


def test_switch_waits_bounded_time_and_prints_diagnostics_before_rollback():
    source = SWITCH.read_text()
    assert 'READY_TIMEOUT=${PREVIEW_READY_TIMEOUT_SECONDS:-600}' in source
    assert "(( READY_TIMEOUT >= 60 && READY_TIMEOUT <= 1800 ))" in source
    assert 'tail -n 60 "$LOG_PATH"' in source
    # The final decision still requires every original condition.
    for condition in ('h.get("status")=="HEALTHY"', 'r.get("status")=="READY"',
                      'get("execution_capability") is False', 'r.get("contract_quotes_fresh") is True'):
        assert condition in source, condition


def test_unexpected_opening_generation_error_is_contained(tmp_path, monkeypatch):
    import src.signals_display.server as server_module

    calls = []

    def broken(*_args, **_kwargs):
        calls.append(1)
        raise ZeroDivisionError("unexpected feature arithmetic")

    class VerifiedFeed:
        def poll(self):
            data = frame()
            data["markets"][0].update(mapping_verified=True, averaging_rule_verified=True,
                                      comparison_operator=">=")
            return data

    monkeypatch.setattr(server_module, "opening_forecast", broken)
    monitor = Monitor(VerifiedFeed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    state = monitor.snapshot()
    assert calls, "the opening path must actually run in this regression test"
    assert not state.get("connection_error"), state.get("warnings")
    initial = (state.get("prediction_engine") or {}).get("initial") or {}
    # Shown as unavailable: WAIT and no probability. Never an invented call.
    assert initial.get("status") == "UNAVAILABLE" and initial.get("up") is None
    assert initial.get("direction") == "WAIT"

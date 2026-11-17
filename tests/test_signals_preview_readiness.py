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


def test_switch_readiness_probe_passes_a_genuinely_ready_service_and_reports_startup():
    def run(responses):
        _Server.responses = responses
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            script = _probe_source() + f'\nPYTHON={sys.executable}\nPREVIEW_PROBE_PORT={server.server_address[1]}\nreadiness_report abc\n'
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        finally:
            server.shutdown()

    healthy = {
        "/healthz": (200, {"status": "HEALTHY", "build_commit": "abc", "signals_only": True,
                           "workers": {"healthy": True}}),
        "/readyz": (200, {"status": "READY", "signals_only": True, "contract_quotes_fresh": True,
                          "reference_fresh": True}),
        "/api/candle-integrity": (200, {"status": "READY", "bars": 1440, "unique_bars": 1440}),
        "/api/state?view=main": (200, {"decision_intelligence": {"execution_capability": False}}),
    }
    ready = run(healthy)
    assert ready.returncode == 0 and "READINESS PASS" in ready.stdout
    # Temporary startup: the startup handler answers 503 with its loading phase.
    startup = dict(healthy, **{"/healthz": (503, {"status": "STARTING", "phase": "LOADING_JOURNAL"}),
                               "/readyz": (503, {"status": "STARTING", "phase": "LOADING_JOURNAL"})})
    starting = run(startup)
    assert starting.returncode != 0 and "LOADING_JOURNAL" in starting.stdout


def test_core_market_data_failure_still_makes_the_service_not_ready(tmp_path):
    class FailingFeed:
        def poll(self):
            raise RuntimeError("exchange unavailable")

    monitor = Monitor(FailingFeed(), Journal(tmp_path / "journal.db"))
    monitor.tick()
    state = monitor.snapshot()
    assert state["stale"] and state.get("connection_error")  # genuine failure is never masked


# ---- 902e825 failure: live candle window vs frozen opening, snapshot coherence ----

from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from src.signals_display.server import make_handler
from tests.test_signals_opening_models import opening as frozen_opening


def _probe(responses):
    _Server.responses = responses
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = _probe_source() + f'\nPYTHON={sys.executable}\nPREVIEW_PROBE_PORT={server.server_address[1]}\nreadiness_report abc\n'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()


def _responses(candles, initial):
    return {
        "/healthz": (200, {"status": "HEALTHY", "build_commit": "abc", "signals_only": True,
                           "workers": {"healthy": True}}),
        "/readyz": (200, {"status": "READY", "signals_only": True, "contract_quotes_fresh": True,
                          "reference_fresh": True}),
        "/api/candle-integrity": (200, candles),
        "/api/state?view=main": (200, {"decision_intelligence": {"execution_capability": False},
                                       "prediction_engine": {"initial": initial}}),
    }


GAP = {"status": "INCOMPLETE", "missing_intervals": [1794887760], "bars": 1439, "unique_bars": 1439}


def test_live_gap_after_a_complete_frozen_opening_is_ready():
    complete = frozen_opening()  # real opening record built from complete pre-open history
    assert complete["up"] is not None
    assert complete["data_quality"]["closed_history_bars"] == 1440
    assert complete["frozen_features"]["closed_history_bars"] == 1440
    result = _probe(_responses(GAP, complete))
    assert result.returncode == 0 and "READINESS PASS" in result.stdout, result.stdout


def test_withheld_and_rollover_placeholder_openings_pass_with_a_live_gap():
    for initial in ({"up": None, "direction": "WAIT"},
                    {"status": "INSUFFICIENT DATA", "up": None, "reason": "No frozen opening inputs available"}):
        result = _probe(_responses(GAP, initial))
        assert result.returncode == 0, (initial, result.stdout)


def test_probability_without_complete_frozen_provenance_still_fails():
    complete = frozen_opening()
    partial = json.loads(json.dumps(complete))
    partial["data_quality"]["closed_history_bars"] = 1439
    late = json.loads(json.dumps(complete))
    late["frozen_features"]["max_input_received_at"] = (
        datetime.fromisoformat(complete["opens_at"]) + timedelta(seconds=5)).isoformat()
    unknown = {"up": 0.61, "direction": "WAIT"}  # probability with no provenance at all
    for bad in (partial, late, unknown):
        result = _probe(_responses(GAP, bad))
        assert result.returncode != 0 and "complete-frozen opening" in result.stdout


def test_stale_candle_collector_never_passes_even_with_a_valid_opening():
    stale = dict(GAP, status="STALE")
    result = _probe(_responses(stale, frozen_opening()))
    assert result.returncode != 0


def test_no_opening_probability_is_generated_from_an_incomplete_history():
    from tests.test_signals_display import NOW
    from tests.test_signals_prediction_engine import history, market, points
    from src.signals_display import prediction_engine as engine

    data = history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1))
    data["candles"] = [bar for i, bar in enumerate(data["candles"]) if i != 700]  # one genuine gap
    result = engine.opening_forecast(market(), data, points(), NOW + timedelta(seconds=2))
    assert result["up"] is None and result["direction"] not in {"UP", "DOWN"}
    assert "Incomplete 24h window" in result["reason"]


def _integrity(monitor, now):
    import src.signals_display.server as server_module
    original = server_module.utcnow
    server_module.utcnow = lambda: now
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/candle-integrity", timeout=5) as response:
            return json.load(response)
    finally:
        server.shutdown()
        server_module.utcnow = original


def test_candle_integrity_uses_the_cache_cutoff_across_minute_boundaries(tmp_path):
    received = datetime(2026, 11, 17, 3, 56, 30, tzinfo=timezone.utc)
    cutoff = int(received.timestamp()) // 60 * 60
    monitor = Monitor(None, Journal(tmp_path / "integrity.db"))
    monitor.latest_candles = {
        "received_at": received.isoformat(), "diagnostics": {"status": "READY"},
        "candles": [{"start": t, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
                    for t in range(cutoff - 86400, cutoff, 60)],
    }
    # 35 s later the wall clock has crossed 03:57; the cache is still coherent.
    later = _integrity(monitor, received + timedelta(seconds=35))
    assert later["status"] == "READY" and later["missing_intervals"] == []
    assert later["cutoff"] == cutoff and later["wall_clock_cutoff"] == cutoff + 60
    # A genuine gap inside the cache's own window is still reported exactly.
    gap = cutoff - 300
    monitor.latest_candles["candles"] = [b for b in monitor.latest_candles["candles"] if b["start"] != gap]
    assert _integrity(monitor, received + timedelta(seconds=35))["missing_intervals"] == [gap]
    # A cache older than 150 s is STALE, never READY.
    monitor.latest_candles["candles"].append({"start": gap, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    assert _integrity(monitor, received + timedelta(seconds=200))["status"] == "STALE"

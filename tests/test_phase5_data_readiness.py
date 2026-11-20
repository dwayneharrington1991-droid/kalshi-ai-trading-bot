"""Phase 5: read-only data-quality audit, pre-open quote probe, per-input status."""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import data_readiness as readiness
from src.signals_display import kalshi_preopen_probe as probe
from tests.test_signals_settled_audit import ASOF, START, _db, _opening, _ro

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "f52e142247d2e6d41a3cf60d3b4864c9a8400aa4"

SCENARIO = r"""
import json, sys
from datetime import timedelta
sys.path.insert(0, sys.argv[1])
from tests.test_signals_display import NOW
from tests.test_signals_opening_models import opening
from tests.test_signals_opening_pipeline_repairs import _context, _flow
out = {}
for name, ctx in (("none", None), ("live", _context(order_flow=_flow(NOW - timedelta(seconds=2)))),
                  ("post_open", _context(order_flow=_flow(NOW + timedelta(seconds=1)))),
                  ("stale", _context(order_flow=_flow(NOW - timedelta(seconds=9))))):
    o = opening(frozen_context=ctx) if ctx is not None else opening()
    o["frozen_features"].pop("optional_feature_status", None)
    o["frozen_features"].pop("optional_context_available", None)
    out[name] = o
print(json.dumps(out, sort_keys=True, default=str))
"""


def _run(tree, tmp_path):
    script = tmp_path / "scenario.py"
    script.write_text(SCENARIO)
    result = subprocess.run([sys.executable, str(script), str(tree)], cwd=tree,
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_per_input_status_is_metadata_only_versus_the_approved_base(tmp_path):
    if shutil.which("git") is None or subprocess.run(
            ["git", "cat-file", "-e", BASE_COMMIT], cwd=ROOT).returncode:
        pytest.skip("approved base commit not available")
    base = tmp_path / "base"
    base.mkdir()
    archive = subprocess.run(["git", "archive", BASE_COMMIT, "src", "tests"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(base)], input=archive, check=True)
    before, after = _run(base, tmp_path), _run(ROOT, tmp_path)
    # Openings are generated at fixture time; generated_at-derived fields are deterministic in
    # these fixtures, so the whole record (values, probability, direction, receipts) must match.
    assert after == before


def test_per_input_status_explains_present_absent_and_excluded_inputs():
    from datetime import timedelta as td
    from tests.test_signals_display import NOW
    from tests.test_signals_opening_models import opening
    from tests.test_signals_opening_pipeline_repairs import _context, _flow

    live = opening(frozen_context=_context(order_flow=_flow(NOW - td(seconds=2))))["frozen_features"]
    assert live["optional_feature_status"]["spot_cvd_delta_fraction"] == "PRESENT"
    assert live["optional_context_available"] is True
    stale = opening(frozen_context=_context(order_flow=_flow(NOW - td(seconds=9))))["frozen_features"]
    assert stale["optional_feature_status"]["spot_cvd_delta_fraction"] == "EXCLUDED:STALE_PRE_OPENING_ORDER_FLOW_EXCLUDED"
    plain = opening()["frozen_features"]
    assert plain["optional_context_available"] is False and plain["optional_feature_status"] == {}
    assert live["version"] == plain["version"] == "opening-asof-features-v2"  # training compatibility unchanged


# ------------------------------------------------------------- data readiness


def _failed(db, i, reason, delay):
    """An immutable probability-less opening exactly as save_opening stores it."""
    opened = START + timedelta(minutes=15 * i)
    closed = opened + timedelta(minutes=15)
    ticker = f"KXBTC15M-{i:04d}"
    payload = {"ticker": ticker, "opens_at": opened.isoformat(), "closes_at": closed.isoformat(),
               "up": None, "direction": "UNCLEAR", "status": "INSUFFICIENT DATA", "reason": reason,
               "model": "opening24h-zero-drift-final-average-v1", "opening_delay_seconds": delay,
               "confidence": {"status": "UNCALIBRATED"}}
    db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?)",
               ("PUBLIC MARKET DATA", ticker, (opened + timedelta(seconds=delay)).isoformat(),
                opened.isoformat(), closed.isoformat(), None, json.dumps(payload)))
    db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
               (ticker, "official", (closed + timedelta(seconds=60)).isoformat(), 1))


def _journal(tmp_path):
    def build(db):
        db.execute("CREATE TABLE btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT,"
                   "PRIMARY KEY(source,start))")
        _failed(db, 0, "Incomplete 24h window: 1439/1440 closed bars; missing UTC intervals: x", 121)
        _failed(db, 1, "No fresh BTC reference received by opening", 2400)  # restart-like
        for i in range(2, 10):
            row = _opening(db, i, 0.7, "UP", outcome=1)
            db.execute("UPDATE btc_engine_opening SET payload=json_set(payload,'$.model','opening-candidate/abc') "
                       "WHERE ticker=?", (row,))
        _failed(db, 10, "No fresh BTC reference received by opening", 125)
        # Contract 0's full 24 h window WAS received before its open: a timing defect, not missing data.
        opened = int(START.timestamp())
        db.executemany("INSERT INTO btc_candles VALUES (?,?,?,?)", [
            ("Coinbase BTC-USD", m, "{}", datetime.fromtimestamp(m + 62, timezone.utc).isoformat())
            # The minute closing AT the open arrives 2 s after it, exactly as in production, so
            # context24's window is the 1,440 minutes ending one minute before the open.
            for m in range(opened - 86460, opened, 60)])
        db.execute("INSERT INTO forecast_outcomes VALUES ('KXBTC15M-NOREC','official',?,1)", (START.isoformat(),))
    return _db(tmp_path, build)


def test_unavailable_openings_are_classified_by_cause_timing_phase_and_evidence(tmp_path):
    with _ro(_journal(tmp_path)) as db:
        report = readiness.audit(db, ASOF)
    u = report["unavailable_openings"]
    assert u["settled_without_probability"] == 3 and u["settled_contracts_with_no_opening_record"] == 1
    assert u["by_category"] == {"REFERENCE_MISSING_AT_OPEN": 2, "CANDLES_INCOMPLETE_24H": 1}
    assert u["by_timing"] == {"IN_PROCESS_AT_GRACE_DEADLINE": 2, "CREATED_LATE_PROCESS_NOT_TICKING_AT_OPEN": 1}
    # Activation inferred from the first opening made by an opening-candidate model (#2).
    assert u["by_phase"] == {"BEFORE_CURRENT_MODEL_ACTIVE": 2, "AFTER_CURRENT_MODEL_ACTIVE": 1}
    assert u["genuine_collection_failures_after_activation"] == 1
    candle = next(e for e in u["records"] if e["category"] == "CANDLES_INCOMPLETE_24H")
    assert candle["coinbase_minutes_recorded_by_open"] == 1440
    assert candle["candle_diagnosis"] == "RECORDED_BY_OPEN_TIMING_OR_SNAPSHOT_DEFECT"
    codes = {a["code"] for a in report["alerts"]}
    assert {"OPENING_PROBABILITY_UNAVAILABLE_RATE", "OPENINGS_CREATED_LATE",
            "SETTLED_WITHOUT_OPENING_RECORD", "NO_PRE_OPEN_EXECUTABLE_QUOTES"} <= codes


def test_reason_classifier_covers_every_opening_failure_message():
    cases = {
        "Incomplete 24h window: 1400/1440": "CANDLES_INCOMPLETE_24H",
        "Candle receipt stale/future-dated": "CANDLES_RECEIPT_STALE",
        "Latest complete historical minute is too old": "CANDLES_LAST_MINUTE_TOO_OLD",
        "Historical snapshot received after opening is excluded": "CANDLES_RECEIVED_AFTER_OPEN",
        "No fresh BTC reference received by opening": "REFERENCE_MISSING_AT_OPEN",
        "Unsupported official settlement rules": "SETTLEMENT_RULES_UNVERIFIED",
        "No valid frozen opening inputs": "NO_FROZEN_INPUTS",
        "'received_at'": "INPUT_FIELD_MISSING",
        "something new": "OTHER",
    }
    for text, category in cases.items():
        assert readiness.classify_reason(text) == category, text


def test_sparse_inputs_are_diagnosed_by_source_and_recording(tmp_path):
    def build(db):
        for i in range(10):
            _opening(db, i, 0.7, "UP", outcome=1)
        db.execute("""UPDATE btc_engine_opening SET payload=json_set(payload,
            '$.frozen_features.values', json('{"return_5m": 0.1, "spot_cvd_delta_fraction": null}'),
            '$.frozen_features.optional_context_status', 'UNAVAILABLE: no frozen microstructure context')""")
    with _ro(_db(tmp_path, build)) as db:
        report = readiness.audit(db, ASOF, {"model": {"preprocessor": {
            "names": ["return_5m", "spot_cvd_delta_fraction", "opening_ema200_distance"]}}})
    sparse = report["sparse_inputs"]["sparse_inputs"]
    assert "return_5m" not in sparse
    assert sparse["spot_cvd_delta_fraction"]["source_group"] == "kraken_order_flow"
    assert sparse["spot_cvd_delta_fraction"]["diagnosis"].startswith("SOURCE_CONTEXT_ABSENT_AT_OPEN")
    assert sparse["opening_ema200_distance"]["diagnosis"].startswith("NOT_RECORDED_BY_THE_LIVE_CODE")


def test_quote_coverage_distinguishes_pre_open_executable_from_post_open(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1, late_quote=True)
    with _ro(_db(tmp_path, build)) as db:
        q = readiness.quote_coverage(db)
    assert q["executable_two_sided_rows_at_or_before_open"] == 0
    assert q["rows_by_seconds_from_open"]["0s_to_10s"] == 1
    assert q["diagnosis"].startswith("NO_PRE_OPEN_QUOTE_ROWS")


def test_readiness_projection_counts_report_b_requirement(tmp_path):
    with _ro(_journal(tmp_path)) as db:
        r = readiness.audit(db, ASOF)["readiness"]
    need = r["eligible_needed_for_report_b"]
    assert need - round(need * 0.2) >= 230 and (need - 1) - round((need - 1) * 0.2) < 230


def test_data_readiness_cli_is_read_only_and_refuses_overwrite(tmp_path):
    path = _journal(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    out = tmp_path / "readiness.json"
    run = lambda: subprocess.run([sys.executable, "-m", "src.signals_display.data_readiness", "--journal", str(path),
                                  "--output", str(out), "--asof", ASOF.isoformat()],
                                 cwd=ROOT, capture_output=True, text=True, timeout=120)
    first = run()
    assert first.returncode == 0, first.stderr
    assert "ALERT" in first.stdout and json.loads(out.read_text())["writes_performed"] == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    foreign = tmp_path / "artifact.json"
    foreign.write_text(json.dumps({"model_id": "x"}))
    refused = subprocess.run([sys.executable, "-m", "src.signals_display.data_readiness", "--journal", str(path),
                              "--output", str(foreign)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert refused.returncode != 0 and json.loads(foreign.read_text()) == {"model_id": "x"}


# ------------------------------------------------------------ pre-open probe


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


def _listing(now, status, yes_ask):
    opens = now + timedelta(seconds=40)
    return {"markets": [{"ticker": "KXBTC15M-NEXT", "status": status, "open_time": opens.isoformat(),
                         "close_time": (opens + timedelta(minutes=15)).isoformat(),
                         "yes_bid_dollars": "0.0000", "yes_ask_dollars": yes_ask,
                         "no_bid_dollars": "0.0000", "no_ask_dollars": "1.0000"}]}


@pytest.mark.parametrize("status,book,expected", [
    ("initialized", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, False),
    ("active", {"orderbook_fp": {"yes_dollars": [["0.48", "10"]], "no_dollars": [["0.50", "7"]]}}, True),
    ("active", {"orderbook": {"yes": [[48, 10]], "no": [[50, 7]]}}, True),  # legacy cents
])
def test_probe_counts_only_tradable_positive_depth_asks_before_open(monkeypatch, status, book, expected):
    start = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    clock = _Clock(start)
    monkeypatch.setattr(probe, "_now", clock)
    monkeypatch.setattr(probe, "_get", lambda base, path, timeout=8:
                        book if path.endswith("/orderbook") else _listing(start, status, "1.0000"))
    report = probe.run("http://unused", 30, 10, sleep=clock.sleep, clock=clock)
    rows = report["observations"]
    assert rows and all(r["kind"] == "OBSERVED" for r in rows)
    assert report["summary"]["executable_quote_seen_before_any_open"] is expected
    if expected:
        assert rows[0]["book"]["yes_ask"] == pytest.approx(0.5) and rows[0]["book"]["yes_ask_size"] == 7
    assert report["summary"]["contracts_observed_before_their_open"] == 1


def test_probe_is_bounded_and_never_overwrites(tmp_path):
    out = tmp_path / "probe.json"
    out.write_text("{}")
    for args in (["--duration-seconds", "10"], ["--interval-seconds", "1"], []):
        result = subprocess.run([sys.executable, "-m", "src.signals_display.kalshi_preopen_probe",
                                 "--output", str(out), *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode != 0
    assert out.read_text() == "{}"


def test_candle_evidence_mirrors_context24_window_and_detects_genuine_gaps(tmp_path):
    opened = START
    open_ts = int(opened.timestamp())

    def build(gap=None, last_received_late=False):
        def inner(db):
            db.execute("CREATE TABLE btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT)")
            for m in range(open_ts - 86460, open_ts, 60):
                if m == gap:
                    continue
                received = datetime.fromtimestamp(m + 62, timezone.utc)
                if last_received_late and m >= open_ts - 300:
                    received = opened + timedelta(seconds=30)  # arrived after the open
                db.execute("INSERT INTO btc_candles VALUES (?,?,?,?)", ("Coinbase BTC-USD", m, "{}", received.isoformat()))
        return inner

    cases = ((build(), 1440), (build(gap=open_ts - 7200), 1439), (build(last_received_late=True), 0))
    for index, (builder, expected) in enumerate(cases):
        folder = tmp_path / str(index)
        folder.mkdir()
        with _ro(_db(folder, builder)) as db:
            assert readiness._candles_recorded_by_open(db, opened) == expected, index


def test_phase5_runner_is_read_only_and_command_targets_it():
    script = (ROOT / "deploy/oracle/data_readiness_readonly.sh").read_text()
    for forbidden in ("systemctl", "kill ", "pkill", "restart", "switch_preview", "install_preview",
                      "INSERT", "UPDATE ", "DELETE", "cp ", "mv "):
        assert forbidden not in script, forbidden
    assert "mode=ro" not in script  # journals are opened read-only inside the Python tools
    assert "/home/opc/kalshi-research/phase5-" in script
    doc = (ROOT / "docs/PHASE5_DATA_READINESS.md").read_text()
    ps = doc.split("```powershell\n", 1)[1].split("\n```", 1)[0]
    assert "BRANCH=claude/phase5-data-readiness" in ps and "data_readiness_readonly.sh" in ps
    assert "StrictHostKeyChecking=yes" in ps and "accept-new" not in ps
    for module in ("data_readiness", "artifact_diagnostics", "opening_research"):
        assert "mode=ro" in (ROOT / "src/signals_display" / f"{module}.py").read_text(), module


def test_recovery_simulation_is_strict_labelled_and_never_counts_unverifiable_as_recovered(tmp_path):
    def build(db):
        db.execute("CREATE TABLE btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT)")
        _failed(db, 0, "Incomplete 24h window: 1439/1440", 121)            # candles provable, ref unknown
        _failed(db, 1, "No fresh BTC reference received by opening", 2400)  # no persisted ticks table
        _failed(db, 2, "Unsupported official settlement rules", 121)
        opened = int(START.timestamp())
        db.executemany("INSERT INTO btc_candles VALUES (?,?,?,?)", [
            ("Coinbase BTC-USD", m, "{}", datetime.fromtimestamp(m + 62, timezone.utc).isoformat())
            for m in range(opened - 86460, opened, 60)])
    with _ro(_db(tmp_path, build)) as db:
        sim = readiness.audit(db, ASOF)["recovery_simulation"]
    assert sim["label"].startswith("SIMULATED RECOVERY")
    assert sim["outcomes"] == {"SIMULATED_RECOVERABLE": 0, "HISTORY_PROVEN_REFERENCE_UNVERIFIABLE": 1,
                               "REFERENCE_UNVERIFIABLE_NO_PERSISTED_TICKS": 1, "NOT_RECOVERABLE": 1}
    assert sim["tick_persistence_present"] is False


def test_recovery_simulation_counts_openings_with_persisted_pre_open_ticks(tmp_path):
    opened = START + timedelta(minutes=15)

    def build(db):
        db.execute("""CREATE TABLE preopen_reference_ticks (mode TEXT NOT NULL,source TEXT NOT NULL,
            received_at TEXT NOT NULL,price REAL NOT NULL,opens_at TEXT NOT NULL,persisted_at TEXT NOT NULL,
            PRIMARY KEY(mode,source,received_at))""")
        for offset, persisted in ((-30, -29), (-3, -2)):
            db.execute("INSERT INTO preopen_reference_ticks VALUES (?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", "Coinbase BTC/USD proxy", (opened + timedelta(seconds=offset)).isoformat(),
                        100000.0, opened.isoformat(), (opened + timedelta(seconds=persisted)).isoformat()))
        _failed(db, 1, "No fresh BTC reference received by opening", 2400)
        _failed(db, 2, "No fresh BTC reference received by opening", 2400)  # no ticks for this opening
    with _ro(_db(tmp_path, build)) as db:
        sim = readiness.audit(db, ASOF)["recovery_simulation"]
    assert sim["tick_persistence_present"] is True
    assert sim["outcomes"]["SIMULATED_RECOVERABLE"] == 1 and sim["outcomes"]["NOT_RECOVERABLE"] == 1

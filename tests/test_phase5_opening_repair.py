"""Phase 5 opening-reliability repairs: retry, crash-safe reference receipts,
restart protection, quote evidence and alerts. Restart simulations drive the
real Monitor.tick path."""
from copy import deepcopy
from datetime import timedelta
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.signals_display import opening_alerts
from src.signals_display import prediction_engine as engine
from src.signals_display.live_model import LiveEstimator
from src.signals_display.monitor import Journal, PublicFeed
from src.signals_display.server import Monitor
from tests.test_signals_display import NOW
from tests.test_signals_monitor import frame
from tests.test_signals_prediction_engine import history, market, points

ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = "589ba72a225607f8b967bdf2660a6623ba1ba7ac"
MODE = "PUBLIC MARKET DATA"


# ------------------------------------------------- crash-safe reference ticks


def test_estimator_buffers_accepted_receipts_and_server_keeps_only_pre_open_ones(tmp_path, monkeypatch):
    estimator = LiveEstimator()
    for seconds in (-71, -70, -30, -1, 0, 40):
        estimator.observe_reference({"kind": "quote", "source": "Coinbase BTC/USD proxy",
                                     "received_at": (NOW + timedelta(seconds=seconds)).isoformat(),
                                     "price": 100000 + seconds}, NOW + timedelta(seconds=seconds))
    drained = estimator.drain_preopen_references()
    assert len(drained) == 6 and estimator.drain_preopen_references() == []
    # Server filter: only receipts within 70 s before a discovered contract's opening.
    from src.signals_display.server import PREOPEN_PERSIST_SECONDS
    kept = [received for _s, received, _p in drained
            if 0 <= (NOW - received).total_seconds() <= PREOPEN_PERSIST_SECONDS]
    assert kept == [NOW + timedelta(seconds=s) for s in (-70, -30, -1, 0)]


def test_preopen_receipts_are_acknowledged_only_after_successful_persistence(tmp_path, monkeypatch):
    """A transient SQLite failure must leave every receipt queued for retry."""
    opened = NOW + timedelta(seconds=30)
    clock = NOW
    monkeypatch.setattr("src.signals_display.server.utcnow", lambda: clock)
    journal = Journal(tmp_path / "retry.db")
    monitor = Monitor(PublicFeed(lambda *_a, **_k: None, multi_venue=False), journal)
    monitor.estimator.pending_preopen_references = [
        ("Coinbase BTC/USD proxy", NOW, 100000.0),
    ]
    payload = frame()
    payload.update(mode=MODE, markets=[{**market(), "opens_at": opened.isoformat()}],
                   stream_observations=[])
    attempts = []

    def fail_once(ticks, mode, persisted_at):
        attempts.append(list(ticks))
        if len(attempts) == 1:
            raise sqlite3.OperationalError("busy")
        return 1

    monkeypatch.setattr(journal, "record_preopen_references", fail_once)
    monitor.tick((deepcopy(payload), clock, clock, 0))
    assert monitor.estimator.peek_preopen_references()  # not lost on failure
    monitor.tick((deepcopy(payload), clock, clock, 0))
    assert len(attempts) == 2 and attempts[1][:len(attempts[0])] == attempts[0]
    assert monitor.estimator.peek_preopen_references() == []


def _ticks(offsets, source="Coinbase BTC/USD proxy"):
    return [(source, NOW + timedelta(seconds=o), 100000 + o, NOW) for o in offsets]


def test_recovery_rejects_post_open_late_persisted_and_out_of_window_receipts(tmp_path):
    journal = Journal(tmp_path / "j.db")
    for offset in (-60, -30, -3):  # each persisted 1 s after its receipt, as live
        journal.record_preopen_references(_ticks([offset]), MODE, NOW + timedelta(seconds=offset + 1))
    # Persisted 40 s after receipt: cannot prove availability before the opening.
    journal.record_preopen_references(_ticks([-40], "Kraken BTC/USD proxy"), MODE, NOW)
    with sqlite3.connect(tmp_path / "j.db") as db:  # a hostile/buggy post-open row
        db.execute("INSERT INTO preopen_reference_ticks VALUES (?,?,?,?,?,?)",
                   (MODE, "Coinbase BTC/USD proxy", (NOW + timedelta(seconds=1)).isoformat(), 1.0,
                    NOW.isoformat(), (NOW + timedelta(seconds=1)).isoformat()))
    pts, source, diag = journal.preopen_reference_points(NOW, MODE)
    assert source == "Coinbase BTC/USD proxy"
    assert [p[0] for p in pts] == [NOW - timedelta(seconds=s) for s in (60, 30, 3)]
    assert diag["rejected"] == {"RECEIVED_AFTER_OPENING": 1, "PERSISTED_TOO_LATE_TO_PROVE_AVAILABILITY": 1}
    # The recorder itself refuses receipts after the opening or beyond 70 s.
    assert journal.record_preopen_references(_ticks([5, -90]), MODE, NOW) == 0


def test_persisted_reference_receipts_are_immutable(tmp_path):
    journal = Journal(tmp_path / "j.db")
    journal.record_preopen_references(_ticks([-10]), MODE, NOW - timedelta(seconds=9))
    with sqlite3.connect(tmp_path / "j.db") as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("UPDATE preopen_reference_ticks SET price=1")
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM preopen_reference_ticks")


def test_inputs_complete_mirrors_the_opening_models_own_checks():
    good = history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1))
    assert Monitor.opening_inputs_complete(good, points(), NOW)
    stale = [(NOW - timedelta(seconds=30), 100000.0)]
    assert not Monitor.opening_inputs_complete(good, stale, NOW)  # reference > 5 s old
    late = history(NOW - timedelta(minutes=1), NOW + timedelta(seconds=1))
    assert not Monitor.opening_inputs_complete(late, points(), NOW)  # history received after open
    gap = deepcopy(good)
    gap["candles"].pop(700)
    assert not Monitor.opening_inputs_complete(gap, points(), NOW)


# ------------------------------------------------------- restart simulations


def _run_process(tmp_path, monkeypatch, *, restart_at=None, persist=True, journal_name="sim.db",
                 opened=None, listed_before_open=True):
    """Drive Monitor.tick through one opening. With restart_at, the first
    process stops just before the open and a NEW process (empty memory) takes
    over at restart_at. Returns the immutable opening record."""
    opened = opened or NOW
    clock = [opened - timedelta(seconds=66)]
    monkeypatch.setattr("src.signals_display.server.utcnow", lambda: clock[0])
    contract = market()
    contract.update(opens_at=opened.isoformat(), closes_at=(opened + timedelta(minutes=15)).isoformat())
    candles = history(opened - timedelta(minutes=1), opened - timedelta(seconds=1))
    journal = Journal(tmp_path / journal_name)
    if not persist:
        monkeypatch.setattr(Journal, "record_preopen_references", lambda self, *a, **k: 0)
    feed = PublicFeed(lambda *_a, **_k: None, multi_venue=False)
    first = Monitor(feed, journal)
    base = frame()
    base.update(mode=MODE, markets=[{**contract}])
    # Pre-open: 13 ticks, 5 s apart, each draining its own 5 receipts (as live).
    # 13 ticks 5 s apart (receipts -69..-5 s), then a final tick at -1 s that
    # also delivers the candle batch received at -1 s.
    for step in range(14):
        now = opened - timedelta(seconds=65 - 5 * step) if step < 13 else opened - timedelta(seconds=1)
        clock[0] = now
        payload = deepcopy(base)
        payload.update(now=now.isoformat(), clock=now.isoformat(),
                       stream_observations=[{"kind": "quote", "source": "Coinbase BTC/USD proxy",
                                             "received_at": (now - timedelta(seconds=4 - i)).isoformat(),
                                             "price": 100000 + 0.1 * (5 * step + i)} for i in range(5)],
                       reference={"price": 100000.0, "source": "Coinbase BTC/USD proxy",
                                  "observed_at": now.isoformat(), "official": False})
        if step == 13:
            payload["btc_candles"] = candles
        payload["markets"][0]["observed_at"] = now.isoformat()
        if not listed_before_open:
            payload["markets"] = []  # Kalshi may list the next contract only once it opens
        first.tick((payload, now, now, 0))
    worker = first
    at = opened + timedelta(seconds=1) if restart_at is None else restart_at
    if restart_at is not None:
        worker = Monitor(PublicFeed(lambda *_a, **_k: None, multi_venue=False), Journal(tmp_path / journal_name))
    for now in (at, max(at, opened + timedelta(seconds=125))):
        clock[0] = now
        payload = deepcopy(base)
        payload.update(now=now.isoformat(), clock=now.isoformat(), stream_observations=[],
                       reference={"price": 100000.0, "source": "Coinbase BTC/USD proxy",
                                  "observed_at": now.isoformat(), "official": False})
        # After the open the collector's cache was received after the open: ineligible.
        payload["btc_candles"] = {**candles, "received_at": (now + timedelta(milliseconds=1)).isoformat()}
        payload["markets"][0]["observed_at"] = now.isoformat()
        worker.tick((payload, now, now, 0))
    return journal.opening_record(contract["ticker"], MODE)


def test_restart_inside_grace_window_recovers_identical_forecast_from_receipts(tmp_path, monkeypatch):
    normal = _run_process(tmp_path, monkeypatch, journal_name="normal.db")
    recovered = _run_process(tmp_path, monkeypatch, restart_at=NOW + timedelta(seconds=20),
                             journal_name="restart.db")
    assert normal["up"] is not None and recovered["up"] is not None
    # Same receipt-proven reference and candles -> the same probability.
    assert recovered["up"] == normal["up"]
    normal_values, recovered_values = (o["frozen_features"]["values"] for o in (normal, recovered))
    core = [k for k in normal_values if not k.startswith("micro_")]
    assert {k: recovered_values[k] for k in core} == {k: normal_values[k] for k in core}
    # Documented limitation: in-memory microstructure windows are not persisted,
    # so after a restart micro_* features come from the receipt-proven reference
    # points (a path the base code already uses when those windows are cold).
    assert any(normal_values[k] != recovered_values[k] for k in normal_values if k.startswith("micro_"))
    # The artifact was loaded after the opening in the new process, so it is
    # (correctly) ineligible: a recovered opening is made by the fallback.
    assert str(recovered["model"]).startswith("opening24h-zero-drift")
    assert "journal-recovered" in str(recovered.get("reference_source"))
    assert recovered["input_recovery"]["reference_points_recovered_from_journal"] is True
    assert engine.context24  # inputs remain cut off at the opening
    assert recovered["data_cutoff"] == NOW.isoformat()
    assert recovered["frozen_features"]["max_input_received_at"] <= NOW.isoformat()


def test_before_fix_behaviour_restart_without_persisted_receipts_loses_the_opening(tmp_path, monkeypatch):
    lost = _run_process(tmp_path, monkeypatch, restart_at=NOW + timedelta(seconds=20), persist=False)
    assert lost["up"] is None and "No fresh BTC reference" in lost["reason"]


def test_first_valid_opening_is_never_overwritten_by_later_ticks(tmp_path, monkeypatch):
    original = _run_process(tmp_path, monkeypatch, journal_name="keep.db")
    journal = Journal(tmp_path / "keep.db")
    candidate = {**original, "up": 0.99, "generated_at": (NOW + timedelta(minutes=3)).isoformat()}
    journal.opening_record(original["ticker"], MODE, candidate, NOW + timedelta(minutes=3))
    assert journal.opening_record(original["ticker"], MODE) == original


SCENARIO = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import pytest
from tests.test_phase5_opening_repair_scenario import run
print(json.dumps(run(sys.argv[2]), sort_keys=True, default=str))
"""


def test_previously_valid_openings_are_computed_exactly_as_before(tmp_path):
    """The repaired code must produce the identical immutable opening for an
    opening the approved base code already produced (no restart, no gap)."""
    if shutil.which("git") is None or subprocess.run(["git", "cat-file", "-e", BASE_COMMIT], cwd=ROOT).returncode:
        pytest.skip("approved base commit not available")
    base = tmp_path / "base"
    base.mkdir()
    archive = subprocess.run(["git", "archive", BASE_COMMIT, "src", "tests"], cwd=ROOT,
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(base)], input=archive, check=True)
    shutil.copy(ROOT / "tests" / "test_phase5_opening_repair_scenario.py", base / "tests")
    script = tmp_path / "scenario.py"
    script.write_text(SCENARIO)
    outputs = []
    for tree, folder in ((base, tmp_path / "b"), (ROOT, tmp_path / "n")):
        folder.mkdir()
        result = subprocess.run([sys.executable, str(script), str(tree), str(folder)], cwd=tree,
                                capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr[-3000:]
        outputs.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert outputs[0]["up"] is not None
    assert outputs[1] == outputs[0]


# ----------------------------------------------------------- deploy guard


def _guard(now_epoch, **env):
    script = 'source deploy/oracle/opening_window_guard.sh; OPENING_GUARD_SLEEP=true opening_window_wait test; echo rc=$?'
    return subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "OPENING_GUARD_NOW_EPOCH": str(now_epoch), **env})


@pytest.mark.parametrize("offset,deferred", [(0, True), (150, True), (151, False), (600, False),
                                             (809, False), (810, True), (899, True)])
def test_planned_restart_waits_from_90s_before_to_150s_after_each_opening(offset, deferred):
    boundary = 1794887100  # a quarter-hour opening
    result = _guard(boundary + offset)
    assert "rc=0" in result.stdout
    assert ("deferred" in result.stderr) is deferred
    assert "allowed outside the opening window" in result.stderr


def test_emergency_override_is_never_blocked_and_rollback_is_unguarded():
    result = _guard(1794887100, PREVIEW_EMERGENCY_OVERRIDE="1")
    assert "rc=0" in result.stdout and "EMERGENCY OVERRIDE" in result.stderr
    switch = (ROOT / "deploy/oracle/switch_preview_8768.sh").read_text()
    guard_at = switch.index('opening_window_wait "preview 8768 switch"')
    assert guard_at < switch.index('kill -TERM "$old_pid"')
    rollback = switch[:guard_at]  # rollback function is defined before the planned stop
    assert "opening_window_wait" not in rollback
    installer = (ROOT / "deploy/oracle/install_preview_8768.sh").read_text()
    assert "opening_window_wait" not in installer  # restore paths stay unguarded


def test_manual_rollback_preserves_previous_python_code_identity():
    switch = (ROOT / "deploy/oracle/switch_preview_8768.sh").read_text()
    assert 'old_pythonpath=${entry#PYTHONPATH=}' in switch
    assert '[[ -n $old_pythonpath ]] || old_pythonpath=$old_cwd' in switch
    assert "printf 'nohup env PYTHONPATH=%q PYTHONNOUSERSITE=1 ' \"$old_pythonpath\"" in switch
    assert "write_rollback_command" in switch


# ------------------------------------------------------- quote evidence


def _quote_market(status="open", up_size=40, observed=NOW + timedelta(seconds=3)):
    return {"ticker": "KXBTC15M-Q", "status": status, "opens_at": NOW.isoformat(),
            "observed_at": observed.isoformat(), "fee_multiplier": 1.0, "fee_type": "quadratic",
            "quotes": {"up": {"bid": 0.50, "ask": 0.52, "ask_size": up_size},
                       "down": {"bid": 0.46, "ask": 0.50, "ask_size": 0}}}


def test_quote_payload_records_source_executability_spread_and_fee(tmp_path):
    journal = Journal(tmp_path / "q.db")
    assert journal.record_opening_quote(_quote_market(), MODE, NOW + timedelta(seconds=4))
    assert journal.record_opening_quote(_quote_market(status="initialized", up_size=0,
                                                      observed=NOW + timedelta(seconds=6)),
                                        MODE, NOW + timedelta(seconds=7))
    with sqlite3.connect(tmp_path / "q.db") as db:
        rows = [json.loads(p) for (p,) in db.execute("SELECT payload FROM opening_quote_snapshots ORDER BY observed_at")]
    depth, untradable = rows
    assert depth["quote_source"] == "DEPTH_REPORTED" and depth["executable"] == {"up": True, "down": True}
    assert depth["ask_size_proven"] == {"up": 40.0, "down": None}
    assert depth["up_spread"] == pytest.approx(0.02) and depth["taker_fee_one_contract"]["up"] == 0.02
    assert untradable["executable"] == {"up": False, "down": False}
    assert untradable["quote_source"] == "LISTING_SIZE_UNKNOWN" and untradable["observed_not_inferred"]


# --------------------------------------------------------------- alerts


def _opening(i, up, reason="ok", values=None, status=None):
    return {"ticker": f"T{i}", "opens_at": (NOW - timedelta(minutes=15 * (10 - i))).isoformat(), "up": up,
            "reason": reason, "frozen_features": {"values": values or {"a": 1.0, "b": None},
                                                  "optional_feature_status": status or {"b": "EXCLUDED:STALE"},
                                                  "optional_context_available": True}}


def test_alerts_cover_missing_repeated_stale_candles_and_feature_receipts():
    openings = [_opening(i, 0.6) for i in range(7)] + [_opening(7, None, "No fresh BTC reference"),
                                                       _opening(8, None, "Incomplete 24h window")]
    report = opening_alerts.compute_alerts(openings, {"status": "INCOMPLETE", "missing_intervals": [1, 2]},
                                           {"Coinbase BTC/USD proxy": 42.0}, ["a", "b"], NOW)
    codes = {a["code"]: a for a in report["alerts"]}
    assert set(codes) == {"MISSING_OPENING_PROBABILITY", "REPEATED_OPENING_FAILURES", "STALE_BTC_FEED",
                          "CANDLE_WINDOW_NOT_READY", "MISSING_FEATURE_RECEIPTS"}
    assert codes["REPEATED_OPENING_FAILURES"]["consecutive"] == 2
    availability = report["latest_opening_model_input_availability"]
    assert availability["by_input"] == {"a": "PRESENT", "b": "MISSING:EXCLUDED:STALE"}
    healthy = opening_alerts.compute_alerts([_opening(i, 0.6, values={"a": 1.0, "b": 2.0}) for i in range(9)],
                                            {"status": "READY"}, {"Coinbase BTC/USD proxy": 1.0}, ["a", "b"], NOW)
    assert healthy["status"] == "OK" and healthy["alerts"] == []


def test_alerts_endpoint_is_read_only_and_does_not_touch_readiness(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from src.signals_display.server import make_handler

    monitor = Monitor(None, Journal(tmp_path / "a.db"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/data-quality-alerts", timeout=5) as r:
            body = json.load(r)
    finally:
        server.shutdown()
    assert body["read_only"] is True and {a["code"] for a in body["alerts"]} >= {"STALE_BTC_FEED"}


def test_incomplete_inputs_are_never_frozen_into_a_permanent_snapshot(tmp_path, monkeypatch):
    """Before the repair the first post-open tick froze whatever it had, so a
    gap at that instant became permanent and retries were futile."""
    opened = NOW + timedelta(seconds=1)
    monkeypatch.setattr("src.signals_display.server.utcnow", lambda: opened)
    journal = Journal(tmp_path / "gap.db")
    monitor = Monitor(PublicFeed(lambda *_a, **_k: None, multi_venue=False), journal)
    payload = frame()
    contract = market()
    payload.update(mode=MODE, markets=[{**contract, "observed_at": opened.isoformat()}],
                   now=opened.isoformat(), clock=opened.isoformat(), stream_observations=[],
                   reference={"price": 100000.0, "source": "Coinbase BTC/USD proxy",
                              "observed_at": opened.isoformat(), "official": False})
    monitor.tick((payload, opened, opened, 0))
    assert journal.opening_snapshot(contract["ticker"], MODE) is None
    assert journal.opening_record(contract["ticker"], MODE) is None  # still retrying, nothing manufactured


def test_recovery_works_when_the_contract_is_only_listed_at_its_quarter_hour_opening(tmp_path, monkeypatch):
    """Review defect: persistence keyed only to listed contracts saved nothing
    when the next contract is not listed before it opens."""
    from datetime import datetime, timezone

    boundary = datetime.fromtimestamp(-(-int(NOW.timestamp()) // 900) * 900, timezone.utc)
    normal = _run_process(tmp_path, monkeypatch, opened=boundary, listed_before_open=False,
                          journal_name="unlisted-normal.db")
    recovered = _run_process(tmp_path, monkeypatch, opened=boundary, listed_before_open=False,
                             restart_at=boundary + timedelta(seconds=20), journal_name="unlisted-restart.db")
    assert recovered["up"] is not None and recovered["up"] == normal["up"]
    assert "journal-recovered" in str(recovered.get("reference_source"))
    with sqlite3.connect(tmp_path / "unlisted-restart.db") as db:
        keys = {row[0] for row in db.execute("SELECT DISTINCT opens_at FROM preopen_reference_ticks")}
    assert keys == {boundary.isoformat()}


def test_one_receipt_can_serve_two_opening_keys(tmp_path):
    journal = Journal(tmp_path / "keys.db")
    other = NOW + timedelta(seconds=30)
    tick = ("Coinbase BTC/USD proxy", NOW - timedelta(seconds=10), 100000.0)
    journal.record_preopen_references([(*tick, NOW), (*tick, other)], MODE, NOW - timedelta(seconds=9))
    assert journal.preopen_reference_points(NOW, MODE)[0] and journal.preopen_reference_points(other, MODE)[0]


def test_quote_diagnostics_failure_never_drops_a_quote(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "q2.db")

    def boom(*_args):
        raise RuntimeError("diagnostic bug")

    monkeypatch.setattr(Journal, "_quote_quality", staticmethod(boom))
    assert journal.record_opening_quote(_quote_market(), MODE, NOW + timedelta(seconds=4))
    with sqlite3.connect(tmp_path / "q2.db") as db:
        payload = json.loads(db.execute("SELECT payload FROM opening_quote_snapshots").fetchone()[0])
    assert payload["quote_quality_error"] == "RuntimeError" and payload["quotes"]


def test_archive_reads_are_memoised_inside_the_grace_window(tmp_path, monkeypatch):
    monitor = Monitor(None, Journal(tmp_path / "memo.db"))
    calls = []
    monkeypatch.setattr(monitor.journal, "opening_inputs", lambda opened: calls.append(opened) or {"candles": []})
    clock = [NOW]
    monkeypatch.setattr("src.signals_display.server.utcnow", lambda: clock[0])
    monitor.archived_opening_inputs(NOW)
    clock[0] = NOW + timedelta(seconds=4)
    monitor.archived_opening_inputs(NOW)
    clock[0] = NOW + timedelta(seconds=6)
    monitor.archived_opening_inputs(NOW)
    assert len(calls) == 2

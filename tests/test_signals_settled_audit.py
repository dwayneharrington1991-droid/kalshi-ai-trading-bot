"""Phase 3 read-only settled audit and sealed-holdout research."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import opening_research as research
from src.signals_display import rigorous_synthetic as synthetic
from src.signals_display import settled_audit as audit
from tests.test_signals_opening_research import _augment

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
ASOF = START + timedelta(days=30)
CAL = {"status": "EXPERIMENTAL CALIBRATED CANDIDATE"}


def _schema(db):
    db.executescript("""
        CREATE TABLE btc_engine_opening (mode TEXT, ticker TEXT, generated_at TEXT, cutoff TEXT,
            closes_at TEXT, up REAL, payload TEXT, PRIMARY KEY(mode, ticker));
        CREATE TABLE forecast_outcomes (ticker TEXT, origin TEXT, observed_at TEXT, outcome INTEGER,
            PRIMARY KEY(ticker, origin));
        CREATE TABLE opening_quote_snapshots (mode TEXT NOT NULL, ticker TEXT NOT NULL,
            observed_at TEXT NOT NULL, opens_at TEXT NOT NULL, seconds_from_open REAL NOT NULL,
            up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL, up_ask_size REAL,
            down_ask_size REAL, fee_multiplier REAL, fee_type TEXT, quote_event_time TEXT,
            payload TEXT NOT NULL, PRIMARY KEY(mode, ticker, observed_at));
    """)


def _opening(db, i, up, direction, *, status=CAL, outcome=None, settle_delay=60,
             mid=None, late_quote=False, received_offset=-1, history_offset=0):
    opened = START + timedelta(minutes=15 * i)
    closed = opened + timedelta(minutes=15)
    ticker = f"KXBTC15M-{i:04d}"
    payload = {
        "opens_at": opened.isoformat(), "closes_at": closed.isoformat(), "up": up,
        "direction": direction, "confidence": status, "model": "m1",
        "decision_policy": {"threshold": 0.6}, "data_cutoff": opened.isoformat(),
        "data_quality": {"closed_history_bars": 1440},
        "history_last_close": opened.timestamp() + history_offset,
        "frozen_features": {"version": 9, "closed_history_bars": 1440,
                            "max_input_received_at": (opened + timedelta(seconds=received_offset)).isoformat()},
    }
    db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?)",
               ("PUBLIC MARKET DATA", ticker, (opened + timedelta(seconds=2)).isoformat(),
                opened.isoformat(), closed.isoformat(), up, json.dumps(payload)))
    if outcome is not None:
        db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                   (ticker, "official", (closed + timedelta(seconds=settle_delay)).isoformat(), outcome))
    if mid is not None:
        db.execute("INSERT INTO opening_quote_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("PUBLIC MARKET DATA", ticker, (opened - timedelta(seconds=10)).isoformat(),
                    opened.isoformat(), -10, mid - 0.01, mid + 0.01, 1 - mid - 0.01, 1 - mid + 0.01,
                    10, 10, 1.0, "quadratic", None, "{}"))
        # The executable ask the call would have paid, shortly after the call.
        db.execute("INSERT INTO opening_quote_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("PUBLIC MARKET DATA", ticker, (opened + timedelta(seconds=5)).isoformat(),
                    opened.isoformat(), 5, mid - 0.01, mid + 0.01, 1 - mid - 0.01, 1 - mid + 0.01,
                    10, 10, 1.0, "quadratic", None, "{}"))
    if late_quote:
        db.execute("INSERT INTO opening_quote_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("PUBLIC MARKET DATA", ticker, (opened + timedelta(seconds=1)).isoformat(),
                    opened.isoformat(), 1, 0.97, 0.98, 0.01, 0.02, 1, 1, 1.0, "q", None, "{}"))
    return ticker


def _db(tmp_path, build):
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as db:
        _schema(db)
        build(db)
    return path


def _ro(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def test_counts_settled_awaiting_missing_and_slot_gaps(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1)
        _opening(db, 1, 0.3, "DOWN", outcome=1)
        _opening(db, 4, 0.5, "WAIT")  # closed long ago, never settled; slots 2-3 missing
        db.execute("INSERT INTO forecast_outcomes VALUES ('KXBTC15M-ORPHAN','official',?,1)",
                   (START.isoformat(),))
    path = _db(tmp_path, build)
    with _ro(path) as db:
        report = audit.audit(db, ASOF)
    j = report["journal_audit"]
    assert j["recorded_openings"] == 3 and j["verified_settled_openings"] == 2
    assert j["missing_settlement_after_grace"] == 1 and j["awaiting_settlement"] == 0
    assert j["missing_15_minute_slots_between_first_and_last"] == 2
    assert j["settled_contracts_without_opening_record"] == 1


def test_displayed_accuracy_scores_only_shown_calibrated_calls(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1)                        # shown, right
        _opening(db, 1, 0.2, "DOWN", outcome=1)                      # shown, wrong
        _opening(db, 2, 0.85, "UP", outcome=1)                       # shown, right, high band
        _opening(db, 3, 0.58, "WAIT", outcome=0)                     # below threshold: not a call
        _opening(db, 4, 0.9, "UP", status={"status": "UNCALIBRATED"}, outcome=0)  # never shown
        _opening(db, 5, 0.4, "UP", outcome=1)                        # contradicts its probability
    path = _db(tmp_path, build)
    with _ro(path) as db:
        d = audit.audit(db, ASOF)["all_settled"]["displayed_accuracy"]
    assert d["settled_openings"] == 6 and d["displayed_calls"] == 3
    assert (d["correct"], d["wrong"]) == (2, 1)
    assert d["up_calls"]["calls"] == 2 and d["down_calls"]["calls"] == 1
    assert d["by_confidence_band"]["0.80-0.90"]["calls"] == 2  # 0.80 (DOWN at 0.2) and 0.85
    assert d["high_confidence_claim"]["claim_supported"] is False  # n far below 100


def test_preopen_mid_never_uses_quotes_after_the_open(tmp_path):
    def build(db):
        _opening(db, 0, 0.6, "UP", outcome=1, late_quote=True)
        _opening(db, 1, 0.6, "UP", outcome=1, mid=0.55)
    path = _db(tmp_path, build)
    with _ro(path) as db:
        rows, _ = audit.load_openings(db, ASOF)
    assert rows[0]["kalshi_preopen_mid"] is None
    assert rows[1]["kalshi_preopen_mid"] == pytest.approx(0.55)


def test_versus_market_uses_common_contracts_and_paired_tests(tmp_path):
    def build(db):
        for i in range(40):
            y = i % 2
            _opening(db, i, 0.65 if y else 0.35, "UP" if y else "DOWN", outcome=y,
                     mid=0.5 if i % 4 else None)
    path = _db(tmp_path, build)
    with _ro(path) as db:
        m = audit.audit(db, ASOF)["all_settled"]["versus_market"]
    assert m["status"] == "MEASURED" and m["contracts_with_both"] == 30
    assert m["scores"]["kalshi_preopen_mid"]["brier"] == pytest.approx(0.25)
    assert m["scores"]["recorded_ai_opening"]["brier"] == pytest.approx(0.35 ** 2)
    assert m["ai_better_than_market_log_loss"]["samples"] == 30
    assert "Diebold-Mariano" in m["ai_better_than_market_log_loss"]["test"]


def test_integrity_checks_flag_leakage_early_settlement_duplicates_and_disagreement(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1, received_offset=+5)         # input after open
        _opening(db, 1, 0.7, "UP", outcome=1, settle_delay=-120)          # settled before close
        _opening(db, 2, 0.7, "UP", outcome=0, history_offset=+60)         # history past open
        db.execute("INSERT INTO forecast_outcomes VALUES ('KXBTC15M-0002','observed',?,1)",
                   (ASOF.isoformat(),))
    path = _db(tmp_path, build)
    with _ro(path) as db:
        checks = audit.audit(db, ASOF)["integrity_checks"]
    assert checks["post_open_input_receipt"]["examples"] == ["KXBTC15M-0000"]
    assert checks["settlement_observed_before_close"]["examples"] == ["KXBTC15M-0001"]
    assert checks["history_ends_after_open"]["examples"] == ["KXBTC15M-0002"]
    assert checks["official_vs_other_origin_disagreements"] == 1
    assert checks["duplicate_ticker_rows"] == 0


def test_labels_unknown_at_audit_time_are_not_scored(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1, settle_delay=10 ** 7)  # settles after ASOF
    path = _db(tmp_path, build)
    with _ro(path) as db:
        report = audit.audit(db, ASOF)
    assert report["journal_audit"]["verified_settled_openings"] == 0


def test_sealed_split_is_last_fifth_and_hash_is_stable(tmp_path):
    def build(db):
        for i in range(50):
            _opening(db, i, 0.6, "UP", outcome=i % 2)
    path = _db(tmp_path, build)
    with _ro(path) as db:
        first = audit.audit(db, ASOF)["sealed_holdout"]
        second = audit.audit(db, ASOF)["sealed_holdout"]
    assert first["development_contracts"] == 40 and first["holdout_contracts"] == 10
    assert first["holdout_starts_at"] == (START + timedelta(minutes=15 * 40)).isoformat()
    assert first == second


def test_baselines_only_use_labels_available_before_each_open():
    rows = [{"ticker": f"T{i}", "opens_at": START + timedelta(minutes=15 * i)} for i in range(4)]
    # Label of contract i is available 16 minutes after its open (after the next open).
    history = [(START + timedelta(minutes=15 * i + 16), y) for i, y in enumerate((1, 1, 0, 1))]
    base = audit.expanding_base_rate(rows, history)
    assert base["T0"] == base["T1"] == 0.5          # nothing known yet
    assert base["T2"] == pytest.approx(2 / 3)       # only contract 0 known
    keep = audit.persistence(rows, history)
    assert "T0" not in keep and "T1" not in keep
    assert keep["T2"] == pytest.approx(0.5)         # last known UP, no repeat history yet


def test_pnl_excludes_unpriced_calls_and_bootstraps_by_day():
    priced = [{"day": f"2026-10-{d:02d}", "pnl": x, "cost": 0.5, "won": x > 0}
              for d in range(1, 11) for x in (0.45, -0.55)]
    out = audit.pnl_summary(priced, unpriced=3)
    assert out["excluded_no_executable_ask"] == 3 and out["priced_calls"] == 20
    assert out["mean_net_pnl_per_contract"] == pytest.approx(-0.05)
    low, high = out["day_block_bootstrap_95"]
    assert low == high == pytest.approx(-0.05)  # identical days: no day-to-day variance
    assert audit.day_block_bootstrap(priced) == audit.day_block_bootstrap(priced)  # seeded


def test_hypothetical_pnl_prices_displayed_calls_at_the_ask_after_the_call(tmp_path):
    def build(db):
        _opening(db, 0, 0.7, "UP", outcome=1, mid=0.50)    # pays 0.51 + fee + 0.01
        _opening(db, 1, 0.7, "UP", outcome=1)              # no quote: excluded
    path = _db(tmp_path, build)
    with _ro(path) as db:
        pnl = audit.audit(db, ASOF)["all_settled"]["hypothetical_pnl"]
    assert pnl["priced_calls"] == 1 and pnl["excluded_no_executable_ask"] == 1
    assert pnl["mean_net_pnl_per_contract"] == pytest.approx(1 - (0.51 + 0.02 + 0.01))


def test_audit_never_modifies_the_journal(tmp_path):
    def build(db):
        for i in range(10):
            _opening(db, i, 0.6, "UP", outcome=i % 2, mid=0.5)
    path = _db(tmp_path, build)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with _ro(path) as db:
        audit.audit(db, ASOF)
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM btc_engine_opening")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_cli_writes_report_a_first_and_audit_only_stops(tmp_path):
    def build(db):
        for i in range(12):
            _opening(db, i, 0.7 if i % 2 else 0.3, "UP" if i % 2 else "DOWN", outcome=i % 2, mid=0.5)
    path = _db(tmp_path, build)
    out = tmp_path / "out" / "opening_research.report.json"
    out.parent.mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "src.signals_display.opening_research", "--journal", str(path),
         "--output", str(out), "--audit-only"],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "REPORT_A_BEGIN" in result.stdout and "REPORT_B_BEGIN" not in result.stdout
    report = json.loads((out.parent / "report_A_settled_audit.json").read_text())
    assert report["format"] == audit.AUDIT_FORMAT and report["activation_allowed"] is False
    assert report["all_settled"]["displayed_accuracy"]["displayed_calls"] == 12
    assert not out.exists()


# --------------------------------------------- sealed-holdout model research


def test_sealed_holdout_models_never_train_on_holdout_rows(monkeypatch):
    rows = _augment(synthetic.opening_rows(400, seed=301, signal=0.5))
    ordered = research.opening_models._validate_rows(rows)
    seal = 320
    holdout = {r["ticker"] for r in ordered[seal:]}
    seen = []

    def spy(train, validation, spec):
        seen.append({r["ticker"] for r in train} | {r["ticker"] for r in validation})
        return lambda row: 0.5

    monkeypatch.setattr(research, "_fit", spy)
    predictions, folds = research.walk_forward(ordered, seal=seal)
    assert folds >= 2 and seen
    assert all(not (s & holdout) for s in seen)
    assert holdout <= set(predictions["compact_offset"])


def test_research_reports_seal_baselines_family_gates_and_one_holm_family():
    rows = _augment(synthetic.opening_rows(420, seed=302, signal=0.6))
    for i, r in enumerate(rows):
        if i % 3 == 0:
            r["kalshi_preopen_mid"] = 0.5
    report = research.research(rows)
    seal = report["sealed_holdout"]
    assert seal["holdout_contracts"] == 84 and seal["tuning_on_holdout"] is False
    comparison = report["model_comparison"]
    assert comparison["compact_offset"]["holdout_contracts"] == 84
    for baseline in ("baseline_expanding_up_rate", "baseline_persistence_last_settled"):
        assert baseline in comparison
    gates = report["feature_family_history"]
    assert gates["order_book_flow_cvd"]["status"] == "INSUFFICIENT_HISTORY"
    family = report["multiple_testing"]
    assert family["family_size"] == len(family["results"]) > len(research.MODELS)
    assert any(name.endswith("vs kalshi_preopen_mid") for name in family["results"])
    chosen = comparison["compact_offset"].get("holdout_at_chosen_threshold")
    if chosen and chosen["calls_with_executable_quote"]:
        assert "net_pnl_day_block_bootstrap_95" in chosen and "max_drawdown" in chosen

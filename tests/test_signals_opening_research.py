"""Read-only opening research tool: costs, thresholds, quotes, walk-forward hygiene.

All rows here are SYNTHETIC; nothing says anything about live accuracy.
"""
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

ROOT = Path(__file__).resolve().parents[1]


def _augment(rows):
    for r in rows:
        mid = min(0.97, max(0.03, r["baseline"]))
        r.update(recorded_up=r["baseline"], recorded_model="opening24h-zero-drift-final-average-v1",
                 recorded_direction="UNCLEAR", recorded_threshold=None,
                 recorded_confidence_status="UNCALIBRATED", regime="similar volatility")
        r["features"].update(regime_rising=0.0, regime_falling=0.0)
        r["quote"] = {"observed_at": r["timestamp"], "up_ask": round(mid + 0.02, 2),
                      "down_ask": round(1 - mid + 0.02, 2), "fee_multiplier": 1.0, "up_mid": mid}
    return rows


def _row(outcome, up_ask=0.5, down_ask=0.52):
    return {"outcome": outcome, "quote": {"up_ask": up_ask, "down_ask": down_ask, "fee_multiplier": 1.0}}


def test_call_metrics_use_executable_ask_fee_and_slippage():
    # UP call at ask 0.50: fee ceil(0.07*0.25*100)/100 = 0.02, slippage 0.01 -> cost 0.53.
    metrics = research.call_metrics([(0.6, _row(1)), (0.6, _row(0))])
    assert metrics["calls"] == 2 and metrics["correct"] == 1 and metrics["accuracy"] == 0.5
    assert metrics["mean_net_pnl_per_contract"] == pytest.approx(((1 - 0.53) + (0 - 0.53)) / 2)
    assert metrics["mean_model_expected_value"] == pytest.approx(0.6 - 0.53)
    # A DOWN call pays the DOWN ask.
    down = research.call_metrics([(0.3, _row(0, down_ask=0.40))])
    assert down["mean_net_pnl_per_contract"] == pytest.approx(1 - (0.40 + 0.02 + 0.01))
    # Missing quotes are excluded from P&L, never treated as free.
    unpriced = research.call_metrics([(0.7, {"outcome": 1, "quote": None})])
    assert unpriced["calls"] == 1 and unpriced["calls_with_executable_quote"] == 0
    assert unpriced["mean_net_pnl_per_contract"] is None


def test_threshold_table_counts_calls_at_each_confidence_level():
    probabilities = [0.50, 0.51, 0.53, 0.56, 0.38, 0.72]
    rows = [_row(1) for _ in probabilities]
    table = research.threshold_table(probabilities, rows)
    assert list(table) == ["0.50", "0.52", "0.55", "0.60", "0.65", "0.70"]
    assert [table[k]["calls"] for k in table] == [5, 4, 3, 2, 1, 1]
    assert table["0.50"]["coverage"] == pytest.approx(5 / 6)


def _journal(path, called_at):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, ticker TEXT, mode TEXT, timestamp TEXT, payload TEXT)")
        def add(at, stale=False, up_ask=0.55, down_ask=0.47):
            payload = {"stale": stale, "market": {"fee_multiplier": 1.0, "quotes": {
                "up": {"bid": up_ask - 0.02, "ask": up_ask}, "down": {"bid": down_ask - 0.02, "ask": down_ask}}}}
            db.execute("INSERT INTO predictions (ticker,mode,timestamp,payload) VALUES (?,?,?,?)",
                       ("KXBTC15M-Q", "PUBLIC MARKET DATA", at.isoformat(), json.dumps(payload)))
        add(called_at - timedelta(seconds=5), up_ask=0.10)      # before the call: never used
        add(called_at + timedelta(seconds=2), stale=True)        # stale: skipped
        add(called_at + timedelta(seconds=4), up_ask=0.56)       # first usable quote
        add(called_at + timedelta(seconds=90), up_ask=0.90)      # outside the window


def test_quote_is_first_fresh_executable_ask_after_the_call(tmp_path):
    called = datetime(2026, 11, 1, 12, 0, 1, tzinfo=timezone.utc)
    _journal(tmp_path / "j.db", called)
    with sqlite3.connect(tmp_path / "j.db") as db:
        quote = research._quote_at(db, {"ticker": "KXBTC15M-Q", "timestamp": called.isoformat()})
        assert quote["up_ask"] == 0.56 and quote["down_ask"] == 0.47
        late = research._quote_at(db, {"ticker": "KXBTC15M-Q", "timestamp": (called + timedelta(seconds=200)).isoformat()})
        assert late is None


def test_walk_forward_predicts_only_contracts_unseen_by_training():
    rows = _augment(synthetic.opening_rows(260, seed=81, signal=0.8))
    predictions, folds = research.walk_forward(rows)
    assert folds >= 1
    first_test = sorted(rows, key=lambda r: r["closes_at"])[research.WALK_START]["ticker"]
    for name, by_ticker in predictions.items():
        assert by_ticker, name
        assert all(t not in {r["ticker"] for r in rows[: research.WALK_START]} for t in by_ticker), name
        assert first_test in by_ticker, name
        assert all(0 < p < 1 for p in by_ticker.values()), name


def test_fold_count_is_bounded_for_large_journals():
    rows = _augment(synthetic.opening_rows(research.WALK_START + 1200, seed=82))
    calls = []
    original = research._fit
    research._fit = lambda train, validation, spec: calls.append(1) or (lambda row: 0.5)
    try:
        _predictions, folds = research.walk_forward(rows)
    finally:
        research._fit = original
    assert folds <= research.MAX_FOLDS


def test_research_report_is_shadow_only_and_reports_insufficient_data():
    report = research.research(_augment(synthetic.opening_rows(120, seed=83)))
    assert report["status"] == "INSUFFICIENT_OUT_OF_SAMPLE_CONTRACTS"
    for flag in ("promotion_allowed", "activation_allowed", "active_model_changed"):
        assert report[flag] is False
    assert report["recorded_live_openings"]["contracts"] == 120


def test_cli_opens_journal_read_only_and_refuses_foreign_output(tmp_path):
    journal = tmp_path / "signals_monitor.db"
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE forecast_outcomes (ticker TEXT,origin TEXT,observed_at TEXT,outcome INTEGER)")
        db.execute("CREATE TABLE btc_engine_opening (mode TEXT,ticker TEXT,generated_at TEXT,cutoff TEXT,"
                   "closes_at TEXT,up REAL,status TEXT,payload TEXT)")
    before = hashlib.sha256(journal.read_bytes()).hexdigest()
    output = tmp_path / "report.json"
    subprocess.run([sys.executable, "-m", "src.signals_display.opening_research", "--journal", str(journal),
                    "--output", str(output)], cwd=ROOT, check=True, capture_output=True)
    assert hashlib.sha256(journal.read_bytes()).hexdigest() == before
    assert json.loads(output.read_text())["format"] == research.REPORT_FORMAT
    foreign = tmp_path / "opening_shadow.json"
    foreign.write_text(json.dumps({"format": "kxbtc-opening-shadow-v1"}))
    result = subprocess.run([sys.executable, "-m", "src.signals_display.opening_research", "--journal", str(journal),
                             "--output", str(foreign)], cwd=ROOT, capture_output=True)
    assert result.returncode != 0
    assert json.loads(foreign.read_text()) == {"format": "kxbtc-opening-shadow-v1"}


def test_switch_reports_the_artifact_the_running_process_loaded():
    source = (ROOT / "deploy" / "oracle" / "switch_preview_8768.sh").read_text()
    assert 'printf \'OPENING_ARTIFACT=%s\\n\' "${running_artifact:-NONE_SHADOW_FALLBACK_ONLY}"' in source
    assert 'mapfile -d \'\' -t final_argv <"/proc/$new_pid/cmdline"' in source
    assert "&& running_artifact=" not in source  # set -e safe loop


def test_readonly_research_command_and_script_are_valid_and_change_nothing(tmp_path):
    import base64
    script = (ROOT / "deploy" / "oracle" / "research_readonly.sh").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "oracle" / "research_readonly.sh")], check=True)
    for forbidden in ("kill ", "systemctl", "rm -rf", "nohup", "switch_preview", "--account-readonly"):
        assert forbidden not in script, forbidden
    assert "?mode=ro" in (ROOT / "src" / "signals_display" / "opening_research.py").read_text()
    doc = (ROOT / "docs" / "ORACLE_READONLY_RESEARCH.md").read_text()
    ps = doc[doc.index("```powershell\n") + 14: doc.rindex("```")]
    body = ps[ps.index("$bootstrap = @'\n") + 16: ps.index("\n'@\n")]
    sha = "0123456789abcdef0123456789abcdef01234567"
    bootstrap = body.replace("__SHA__", sha)
    path = tmp_path / "bootstrap.sh"
    path.write_text(bootstrap)
    subprocess.run(["bash", "-n", str(path)], check=True)
    encoded = base64.b64encode(bootstrap.encode()).decode()
    assert subprocess.run(["bash", "-c", f"echo {encoded} | base64 -d"], capture_output=True,
                          text=True).stdout == bootstrap
    assert "git -C \"$REPO\" archive \"$SHA\" | tar -x -C \"$code\"" in bootstrap
    assert "trap 'rm -rf \"$code\"' EXIT" in bootstrap
    assert "install_preview_8768" not in bootstrap and "8765" not in bootstrap
    assert ps.count("__FINAL_SHA__") == 1 and ps.count("{") == ps.count("}")
    assert "| Tee-Object -Variable captured | Out-Host" in ps  # progress stays visible

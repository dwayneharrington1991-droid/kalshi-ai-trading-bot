"""Read-only opening research tool: costs, thresholds, quotes, walk-forward hygiene.

All rows here are SYNTHETIC; nothing says anything about live accuracy.
"""
import hashlib
import math
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
    assert list(table) == ["0.50", "0.52", "0.55", "0.60", "0.65", "0.70", "0.75", "0.80", "0.85", "0.90"]
    assert [table[k]["calls"] for k in table] == [5, 4, 3, 2, 1, 1, 0, 0, 0, 0]
    assert table["0.50"]["coverage"] == pytest.approx(5 / 6)


def test_recorded_metrics_score_only_actual_displayed_directional_calls():
    rows = [
        {**_row(1), "recorded_up": 0.7, "recorded_direction": "UP", "recorded_confidence_status": "CALIBRATED"},
        {**_row(0), "recorded_up": 0.3, "recorded_direction": "DOWN", "recorded_confidence_status": "CALIBRATED"},
        {**_row(1), "recorded_up": 0.8, "recorded_direction": "WAIT", "recorded_confidence_status": "CALIBRATED"},
        {**_row(1), "recorded_up": 0.8, "recorded_direction": "UP", "recorded_confidence_status": "NO VERIFIED EDGE"},
        {**_row(1), "recorded_up": 0.2, "recorded_direction": "UP", "recorded_confidence_status": "CALIBRATED"},
    ]
    metrics = research.recorded_decision_metrics(rows)
    assert metrics["calls"] == 2 and metrics["correct"] == 2
    assert metrics["wait_or_withheld"] == 2
    assert metrics["invalid_direction_records"] == 1


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
    report = json.loads(output.read_text())
    assert report["format"] == research.REPORT_FORMAT
    assert report["authoritative_dataset_coverage"]["official_settled_contracts"] == 0
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


def test_holm_adjustment_controls_family_wise_error():
    result = research.holm({"a": 0.01, "b": 0.02, "c": 0.04})
    assert result["a"]["holm_adjusted_p"] == pytest.approx(0.03)
    assert result["b"]["holm_adjusted_p"] == pytest.approx(0.04)
    assert result["c"]["holm_adjusted_p"] == pytest.approx(0.04)
    assert all(item["significant"] for item in result.values())
    assert not research.holm({"a": 0.03, "b": 0.04})["a"]["significant"]


def test_opening_quote_snapshots_are_preferred_and_old_fee_gaps_are_labelled(tmp_path):
    from src.signals_display.monitor import Journal

    called = datetime(2026, 11, 1, 12, 0, 1, tzinfo=timezone.utc)
    journal = Journal(tmp_path / "j.db")
    market = {"ticker": "KXBTC15M-Q", "opens_at": called.isoformat(),
              "observed_at": (called + timedelta(seconds=2)).isoformat(), "fee_multiplier": 1.0,
              "quotes": {"up": {"bid": 0.5, "ask": 0.53}, "down": {"bid": 0.45, "ask": 0.49}}}
    assert journal.record_opening_quote(market, "PUBLIC MARKET DATA", called + timedelta(seconds=3))
    with sqlite3.connect(tmp_path / "j.db") as db:
        quote = research._snapshot_quote_at(db, {"ticker": "KXBTC15M-Q", "timestamp": called.isoformat()})
    assert quote["up_ask"] == 0.53 and quote["source"] == "opening_quote_snapshots"
    assert quote["fee_multiplier_assumed"] is False
    # Older prediction evidence without a recorded fee multiplier: standard schedule, labelled.
    _journal(tmp_path / "old.db", called)
    with sqlite3.connect(tmp_path / "old.db") as db:
        db.execute("UPDATE predictions SET payload=json_remove(payload,'$.market.fee_multiplier')")
        old = research._quote_at(db, {"ticker": "KXBTC15M-Q", "timestamp": called.isoformat()})
    assert old["fee_multiplier"] == research.ASSUMED_FEE_MULTIPLIER and old["fee_multiplier_assumed"] is True


def test_research_preprocess_keeps_research_only_features():
    rows = [{"features": {"regime_rising": float(i % 2), "return_5m": i * 0.001}} for i in range(20)]
    prep = research._preprocess(rows, ("return_5m", "regime_rising", "missing_feature"))
    assert prep["names"] == ["return_5m", "regime_rising"]


def test_preopen_market_price_uses_only_quotes_received_by_the_call(tmp_path):
    from src.signals_display.monitor import Journal

    opened = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    journal = Journal(tmp_path / "j.db")
    def book(seconds, bid, ask):
        observed = opened + timedelta(seconds=seconds)
        market = {"ticker": "KXBTC15M-P", "opens_at": opened.isoformat(), "observed_at": observed.isoformat(),
                  "quotes": {"up": {"bid": bid, "ask": ask}, "down": {"bid": 1 - ask, "ask": 1 - bid + 0.01}}}
        assert journal.record_opening_quote(market, "PUBLIC MARKET DATA", observed + timedelta(seconds=1))
    book(-20, 0.44, 0.48)
    book(-5, 0.50, 0.54)   # latest at or before the call
    book(+3, 0.70, 0.74)   # after the call: never used
    with sqlite3.connect(tmp_path / "j.db") as db:
        # The immutable forecast can be generated after the opening.  The +3 s
        # quote must still be excluded from a feature described as pre-open.
        mid = research._preopen_mid(db, {
            "ticker": "KXBTC15M-P",
            "opens_at": opened.isoformat(),
            "timestamp": (opened + timedelta(seconds=5)).isoformat(),
        })
    assert mid == pytest.approx(0.52)


def test_snapshot_quote_rejects_malformed_executable_prices(tmp_path):
    from src.signals_display.monitor import Journal

    called = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    Journal(tmp_path / "j.db")
    with sqlite3.connect(tmp_path / "j.db") as db:
        db.execute(
            "INSERT INTO opening_quote_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("PUBLIC MARKET DATA", "KXBTC15M-BAD", called.isoformat(), called.isoformat(), 0,
             0.49, 1.2, 0.48, -0.1, 1, 1, 1.0, "quadratic", called.isoformat(), "{}"),
        )
        quote = research._snapshot_quote_at(
            db, {"ticker": "KXBTC15M-BAD", "timestamp": called.isoformat()}
        )
    assert quote is None


def test_inner_cv_preprocessor_is_fit_on_each_past_only_fold(monkeypatch):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(100):
        at = start + timedelta(minutes=15 * i)
        rows.append({
            "ticker": f"T{i}", "timestamp": at.isoformat(),
            "label_available_at": (at + timedelta(minutes=1)).isoformat(),
            "features": {"return_5m": float(i)}, "outcome": i % 2, "baseline": 0.5,
        })
    seen_means = []

    def fake_fit(fit_rows, prep, l2, *, use_offset):
        seen_means.append(prep["means"][0])
        return {
            "preprocessor": prep, "weights": [0.0] * (2 * len(prep["names"]) + 1),
            "use_baseline_offset": use_offset,
        }

    monkeypatch.setattr(research.rigor, "fit_logistic", fake_fit)
    research._fit(rows[:80], rows[80:], {"features": ("return_5m",), "offset": True})
    assert seen_means[0] == pytest.approx(9.5)
    assert seen_means[-1] == pytest.approx(39.5)  # final model uses all training rows


def test_sparse_market_baseline_comparison_uses_only_common_holdout_contracts(monkeypatch):
    rows = _augment(synthetic.opening_rows(260, seed=94, signal=0.2))
    for index, row in enumerate(rows):
        if index % 2 == 0:
            row["kalshi_preopen_mid"] = 0.52
            row["features"]["kalshi_preopen_mid_logit"] = research.learning.logit(0.52)
        else:
            row["kalshi_preopen_mid"] = None

    def fake_walk(ordered, **_kwargs):
        eligible = ordered[research.WALK_START:]
        return {
            name: {row["ticker"]: row["baseline"] for row in eligible}
            for name in research.MODELS
        }, 1

    monkeypatch.setattr(research, "walk_forward", fake_walk)
    report = research.research(rows)
    compact = report["model_comparison"]["compact_offset"]
    market = report["model_comparison"]["kalshi_preopen_mid_as_of_call"]
    assert 0 < market["holdout_contracts"] < compact["holdout_contracts"]
    assert market["holdout_log_loss_vs_baseline"]["samples"] == market["holdout_contracts"]


def test_intracontract_rows_carry_same_time_quotes_and_are_researched_per_bucket():
    rows = []
    for r in synthetic.opening_rows(240, seed=91, signal=0.6):
        r.update(executable_quotes={"up": {"ask": 0.55}, "down": {"ask": 0.47}}, fee_multiplier=None,
                 market=0.53)
        rows.append(r)
    class DB:  # learning.dataset stand-in
        pass
    original = research.learning.dataset
    research.learning.dataset = lambda db, asof: rows
    try:
        buckets = research.live_rows(DB())
    finally:
        research.learning.dataset = original
    assert list(buckets) == [">10m"]
    quote = buckets[">10m"][0]["quote"]
    assert quote["up_ask"] == 0.55 and quote["fee_multiplier_assumed"] is True
    report = research.intracontract_research(buckets)
    assert report[">10m"]["status"] in {"RESEARCH_COMPLETE", "INSUFFICIENT_OUT_OF_SAMPLE_CONTRACTS"}
    assert "immutable opening prediction" in report[">10m"]["scope"]
    assert report[">10m"]["promotion_allowed"] is False


def _retro_journal(path, *, late_bar=False, verified=True):
    opened = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    t0 = int(opened.timestamp())
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE forecast_outcomes (ticker TEXT,origin TEXT,observed_at TEXT,outcome INTEGER)")
        db.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, ticker TEXT, mode TEXT, timestamp TEXT, payload TEXT)")
        db.execute("CREATE TABLE btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT)")
        for ticker, offset, outcome in (("KXBTC15M-R1", 0, 1), ("KXBTC15M-R2", 900, 0)):
            o = opened + timedelta(seconds=offset)
            market = {"opens_at": o.isoformat(), "closes_at": (o + timedelta(minutes=15)).isoformat(),
                      "strike": 100000.0, "mapping_verified": verified, "comparison_operator": ">=",
                      "averaging_rule_verified": True}
            db.execute("INSERT INTO predictions (ticker,mode,timestamp,payload) VALUES (?,?,?,?)",
                       (ticker, "PUBLIC MARKET DATA", (o + timedelta(seconds=5)).isoformat(), json.dumps({"market": market})))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (ticker, "official", (o + timedelta(minutes=16)).isoformat(), outcome))
        import math as _m
        for i, start in enumerate(range(t0 - 86580, t0 + 900, 60)):
            price = 100000 + 30 * _m.sin(i / 17) + i * 0.01
            bar = {"start": start, "open": price, "high": price + 6 + (i % 5), "low": price - 6, "close": price + 2 * _m.sin(i),
                   "volume": 1 + i % 13}
            received = datetime.fromtimestamp(start + 62, timezone.utc)
            if late_bar and start == t0 - 120:
                received = opened + timedelta(seconds=30)  # arrived after the opening
            db.execute("INSERT INTO btc_candles VALUES (?,?,?,?)",
                       ("Coinbase BTC/USD", start, json.dumps(bar), received.isoformat()))
    return opened


def test_retrospective_rows_use_only_candles_received_by_the_opening(tmp_path):
    opened = _retro_journal(tmp_path / "ok.db")
    with sqlite3.connect(tmp_path / "ok.db") as db:
        rows, info = research.retrospective_rows(db, exclude_tickers={"KXBTC15M-R2"})
    assert info["status"] == "RECONSTRUCTED" and [r["ticker"] for r in rows] == ["KXBTC15M-R1"]
    row = rows[0]
    assert row["timestamp"] == opened.isoformat() and row["outcome"] == 1
    assert row["recorded_model"] == "RETROSPECTIVE_RECONSTRUCTION" and row["recorded_up"] is None
    assert 0 < row["baseline"] < 1
    for name in ("return_15m", "rsi_14", "candle_body_range_ratio", "opening_ema50_distance"):
        assert row["features"][name] is not None, name
    # A bar that arrived after the opening is never used: the reconstruction
    # falls back to the last complete window received before the opening.
    _retro_journal(tmp_path / "late.db", late_bar=True)
    with sqlite3.connect(tmp_path / "late.db") as db:
        late_rows, _ = research.retrospective_rows(db, exclude_tickers={"KXBTC15M-R2"})
        t0 = int(opened.timestamp())
        closes = {start: json.loads(payload)["close"] for start, payload in db.execute(
            "SELECT start,payload FROM btc_candles WHERE start IN (?,?,?)", (t0 - 240, t0 - 180, t0 - 120))}
    assert len(late_rows) == 1
    expected = math.log(closes[t0 - 180] / closes[t0 - 240])
    assert late_rows[0]["features"]["return_1m"] == pytest.approx(expected)
    assert late_rows[0]["features"]["return_1m"] != pytest.approx(math.log(closes[t0 - 120] / closes[t0 - 180]))


def test_retrospective_rows_skip_unverified_settlement_rules(tmp_path):
    _retro_journal(tmp_path / "unverified.db", verified=False)
    with sqlite3.connect(tmp_path / "unverified.db") as db:
        rows, info = research.retrospective_rows(db, exclude_tickers=set())
    assert rows == [] and info["skipped"] == {"UNVERIFIED_SETTLEMENT_RULES": 2}


def test_threshold_table_reports_up_down_wait_through_ninety_percent():
    probabilities = [0.95, 0.88, 0.30, 0.52, 0.50]
    table = research.threshold_table(probabilities, [_row(1) for _ in probabilities])
    assert list(table)[-1] == "0.90"
    assert (table["0.90"]["calls"], table["0.90"]["up_calls"], table["0.90"]["wait"]) == (1, 1, 4)
    assert (table["0.70"]["up_calls"], table["0.70"]["down_calls"], table["0.70"]["wait"]) == (2, 1, 2)


def test_condition_report_is_descriptive_with_intervals():
    rows = _augment(synthetic.opening_rows(240, seed=95, signal=0.8))
    report = research.condition_report(rows)
    assert set(report["return_15m"]) == {"low", "mid", "high"}
    assert report["return_15m"]["low"]["contracts"] == 80
    assert len(report["return_15m"]["high"]["up_rate_wilson_95"]) == 2
    assert "Exploratory" in report["note"]

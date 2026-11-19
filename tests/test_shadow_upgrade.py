"""Shadow 8768 upgrade research. All rows are SYNTHETIC: nothing here says
anything about live accuracy."""
import copy
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.signals_display import rigorous_synthetic as synthetic, shadow_upgrade as su
from tests.test_signals_opening_research import _augment

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def signal_report():
    return su.evaluate(_augment(synthetic.opening_rows(520, seed=7, signal=1.2)))


def test_holdout_labels_never_influence_predictions_or_thresholds():
    rows = _augment(synthetic.opening_rows(420, seed=11, signal=1.0))
    a = su.evaluate(copy.deepcopy(rows))
    flipped = copy.deepcopy(rows)
    ordered = sorted(flipped, key=lambda r: (r["closes_at"], r["ticker"]))
    seal = int(len(ordered) * (1 - su.SEALED_HOLDOUT_FRACTION))
    for r in ordered[seal:]:
        r["outcome"] = 1 - r["outcome"]
    b = su.evaluate(flipped)
    assert a["selected_candidate"] == b["selected_candidate"]
    for name in a["candidates"]:
        fa, fb = a["candidates"][name]["accuracy_frontier"], b["candidates"][name]["accuracy_frontier"]
        assert {k: v.get("threshold") for k, v in fa.items()} == {k: v.get("threshold") for k, v in fb.items()}
        ma, mb = a["candidates"][name]["holdout_every_market"], b["candidates"][name]["holdout_every_market"]
        assert ma["calls"] == mb["calls"] and ma["correct"] + mb["correct"] == ma["calls"]


def test_no_signal_never_verifies_high_accuracy():
    report = su.evaluate(_augment(synthetic.opening_rows(420, seed=3, signal=0.0)))
    for entry in report["candidates"].values():
        for target in ("85%", "90%"):
            assert entry["accuracy_frontier"][target]["status"] != "VERIFIED_WILSON_LOWER_BOUND_AT_TARGET"


def test_report_has_accuracy_coverage_and_scores_together(signal_report):
    r = signal_report
    assert r["status"] == "MEASURED" and r["shadow_only"] is True and r["activation_path"] is None
    assert r["selected_candidate"] in su.CANDIDATES
    for entry in r["candidates"].values():
        m = entry["holdout_every_market"]
        assert m["calls"] == r["holdout_contracts"] and 0 < m["brier"] < 0.3 and m["log_loss"] > 0
        for f in entry["accuracy_frontier"].values():
            if "holdout" in f:
                h = f["holdout"]
                assert h["markets"] == r["holdout_contracts"] and 0 <= h["coverage"] <= 1
                assert h["calls"] == h["up_calls"] + h["down_calls"]
    assert set(r["latency"]) >= {"fit_seconds_all_candidates"}
    assert "CURRENT AI" in su.summary(r)


def test_gbdt_learns_a_nonlinear_rule_logistic_cannot():
    rng = random.Random(4)
    rows = _augment(synthetic.opening_rows(600, seed=9, signal=0.0))
    for r in rows:  # UP when both inputs share a sign (XOR-like): no linear signal
        a, b = rng.gauss(0, 1), rng.gauss(0, 1)
        r["features"]["rsi_14"], r["features"]["macd_scaled"] = a, b
        r["outcome"] = int((a * b > 0) if rng.random() < 0.9 else (a * b <= 0))
    train = sorted(rows, key=lambda r: r["closes_at"])
    prep = su.research._preprocess(train[:450], ["rsi_14", "macd_scaled"])
    model = su.fit_gbdt(train[:450], prep)
    right = sum((su.gbdt_raw(model, r["features"]) >= 0.5) == bool(r["outcome"]) for r in train[450:])
    assert right / 150 > 0.75


def test_cli_reads_journal_read_only_and_refuses_unsafe_output(tmp_path):
    db = tmp_path / "j.db"
    sqlite3.connect(db).close()
    bad = subprocess.run([sys.executable, "-m", "src.signals_display.shadow_upgrade", "--journal", str(db),
                          "--output", str(tmp_path / "j.db.json")], cwd=ROOT, capture_output=True, text=True)
    assert bad.returncode != 0
    code = (ROOT / "src/signals_display/shadow_upgrade.py").read_text()
    assert "mode=ro" in code and "PRAGMA query_only=ON" in code
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "model_registry", "activate(", "place_order", "create_order"):
        assert forbidden not in code


def test_live_bot_never_imports_shadow_module():
    for path in (ROOT / "src").rglob("*.py"):
        if path.name not in ("shadow_upgrade.py", "combined_opening.py"):  # shadow modules only
            assert "shadow_upgrade" not in path.read_text(), path


def _shift(value, delta):
    from datetime import datetime
    if isinstance(value, dict):
        return {k: _shift(v, delta) for k, v in value.items()}
    if isinstance(value, list):
        return [_shift(v, delta) for v in value]
    if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
        try:
            return (datetime.fromisoformat(value) + delta).isoformat()
        except ValueError:
            return value
    return value


def test_cli_end_to_end_on_a_real_journal_schema(tmp_path):
    from datetime import timedelta
    from src.signals_display.monitor import Journal
    from tests.test_signals_opening_models import opening, NOW

    journal = Journal(tmp_path / "live.db")
    base = opening()
    rng = random.Random(1)
    with journal.connect() as db:
        for i in range(330):
            row = _shift(copy.deepcopy(base), timedelta(minutes=15 * i))
            row["ticker"] = f"KXBTC15M-E2E{i:04d}"
            values = row["frozen_features"]["values"]
            x = rng.gauss(0, 1)
            values["return_5m"] = 0.001 * x
            outcome = int(rng.random() < 1 / (1 + 2.718 ** (-1.5 * x)))
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", row["ticker"], row["generated_at"], row["data_cutoff"],
                        row["closes_at"], row["up"], row["status"], json.dumps(row)))
            settled = (NOW + timedelta(minutes=15 * i + 16)).isoformat()
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)", (row["ticker"], "official", settled, outcome))
    before = (tmp_path / "live.db").read_bytes()
    out = tmp_path / "report.json"
    asof = (NOW + timedelta(days=10)).isoformat()
    run = subprocess.run([sys.executable, "-m", "src.signals_display.shadow_upgrade", "--journal",
                          str(tmp_path / "live.db"), "--output", str(out), "--asof", asof],
                         cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr[-2000:]
    report = json.loads(out.read_text())
    assert report["contracts"] == 330 and report["status"] == "MEASURED", su.summary(report)
    assert "CURRENT AI" in run.stdout and "REPORT" in run.stdout
    assert (tmp_path / "live.db").read_bytes() == before


def test_oracle_runner_is_read_only_single_job_and_detached():
    path = ROOT / "deploy/oracle/shadow_upgrade_8768.sh"
    code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
    for forbidden in (r"systemctl", r"kill -(TERM|KILL|9|15)", r"\bpkill\b", r"sqlite3 ", r"--port", r"8765",
                      r"git (checkout|reset|pull|merge)", r"rm -rf \"\$J", r"/environ"):
        assert not __import__("re").search(forbidden, code), forbidden
    assert "nice -n 19" in code and "setsid nohup" in code and "--asof" in code
    assert 'if [[ -f $RUN/report.json ]]; then summary; exit 0; fi' in code   # never a second run
    assert "running ||" in code and "kill -0" in code                       # reattach, not restart
    assert all('rm -rf "$RUN/' in l for l in code.splitlines() if "rm -rf" in l)
    assert "^(shadow_upgrade|combined_opening)$" in code            # only the two shadow modules can run
    assert '--previous-report "$PREVIOUS"' in code and "BLOCKED: previous shadow report" in code

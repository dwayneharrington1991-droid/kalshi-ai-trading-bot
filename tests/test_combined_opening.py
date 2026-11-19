"""Shadow combined opening engine. All rows are SYNTHETIC."""
import copy
import json
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import combined_opening as co, learning, rigorous_synthetic as synthetic
from tests.test_signals_opening_research import _augment

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def data(seed=3, signal=1.0, n=240):
    retro = synthetic.opening_rows(400, seed=seed + 100, signal=signal, start=START)
    for r in retro:
        r.update(recorded_up=None, recorded_model="RETROSPECTIVE_RECONSTRUCTION", quote=None)
    live = _augment(synthetic.opening_rows(n, seed=seed, signal=signal, start=START + timedelta(days=6)))
    rng = random.Random(seed)
    for r in live:
        r["recorded_up"] = learning.sigmoid(0.5 * (signal * r["features"]["return_5m"] / 0.0009 + rng.gauss(0, 1)))
    ordered = sorted(live, key=lambda r: r["closes_at"])
    return live, retro, ordered[150]["closes_at"], ordered[195]["closes_at"]


@pytest.fixture(scope="module")
def base():
    live, retro, hs, fa = data()
    return (live, retro, hs, fa), co.evaluate(copy.deepcopy(live), copy.deepcopy(retro), hs, fa)


def _choices(report):
    return (report["selected_combiner"], report["operating_threshold"], json.dumps(report["frontier_development"]),
            json.dumps(report["development"], sort_keys=True))


def test_fresh_and_previous_holdout_labels_never_influence_choices(base):
    (live, retro, hs, fa), report = base
    assert report["status"] == "MEASURED" and report["fresh"]["contracts"] == 44
    for segment in ("fresh", "previous"):
        flipped = copy.deepcopy(live)
        for r in flipped:
            c = r["closes_at"]
            if (segment == "fresh" and c > fa) or (segment == "previous" and hs <= c <= fa):
                r["outcome"] = 1 - r["outcome"]
        other = co.evaluate(flipped, copy.deepcopy(retro), hs, fa)
        if segment == "fresh":
            assert _choices(other) == _choices(report)
        else:  # previous-holdout labels may train fresh-scoring models, never development choices
            assert _choices(other)[0] == _choices(report)[0] and _choices(other)[1:] == _choices(report)[1:]


def test_stacker_only_learns_from_earlier_rows(base):
    (live, retro, hs, fa), _ = base
    rows = sorted(copy.deepcopy(live), key=lambda r: r["closes_at"])[:150]
    oof = co.out_of_fold(rows, [])
    a = co.prequential(rows, oof)
    rows[-1]["outcome"] = 1 - rows[-1]["outcome"]
    b = co.prequential(rows, oof)
    earlier = [r["ticker"] for r in rows[:-1] if r["ticker"] in a["stack"]]
    assert earlier and all(a["stack"][t] == b["stack"][t] for t in earlier)


def test_retrospective_rows_are_training_only(base):
    (live, retro, hs, fa), report = base
    text = json.dumps(report)
    assert not any(r["ticker"] in text for r in retro)
    assert {d["ticker"] for d in report["fresh"]["recent_decisions"]} <= {r["ticker"] for r in live}


def test_decision_is_up_down_or_wait_with_reason():
    bases = {"existing": 0.62, "logistic": 0.58}
    assert co.decision(0.64, 0.60, bases)["direction"] == "UP"
    assert co.decision(0.30, 0.60, bases)["direction"] == "DOWN"
    wait = co.decision(0.56, 0.60, bases)
    assert wait["direction"] == "WAIT" and "below the development-validated threshold" in wait["reason"]
    assert co.decision(0.9, None, bases)["direction"] == "WAIT"
    assert "models agree" in co.decision(0.64, 0.60, bases)["reason"]


def test_report_shows_accuracy_with_coverage_for_every_model(base):
    _, report = base
    f = report["fresh"]
    for key in ("combined_operating", "combined_every_market", "logistic_every_market", "existing_every_market"):
        m = f[key]
        assert m["markets"] == 44 and 0 <= m["coverage"] <= 1 and m["correct"] + m["wrong"] == m["calls"]
    assert set(report["agreement_development"]) == {"agree", "disagree"}
    assert "FRESH UNSEEN CONTRACTS: 44" in co.summary(report)


def test_cli_end_to_end_on_real_journal_schema(tmp_path):
    from tests.test_shadow_upgrade import _shift
    from tests.test_signals_opening_models import opening, NOW
    from src.signals_display.monitor import Journal
    journal = Journal(tmp_path / "live.db")
    base_row, rng = opening(), random.Random(5)
    with journal.connect() as db:
        for i in range(200):
            row = _shift(copy.deepcopy(base_row), timedelta(minutes=15 * i))
            row["ticker"] = f"KXBTC15M-CMB{i:04d}"
            x = rng.gauss(0, 1)
            row["frozen_features"]["values"]["return_5m"] = 0.001 * x
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", row["ticker"], row["generated_at"], row["data_cutoff"],
                        row["closes_at"], row["up"], row["status"], json.dumps(row)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (row["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(),
                        int(rng.random() < 1 / (1 + 2.718 ** (-1.5 * x)))))
    previous = tmp_path / "previous.json"
    closes = [(_shift({"c": base_row["closes_at"]}, timedelta(minutes=15 * i)))["c"] for i in range(200)]
    previous.write_text(json.dumps({"holdout_starts": closes[130], "asof": closes[165]}))
    before = (tmp_path / "live.db").read_bytes()
    run = subprocess.run([sys.executable, "-m", "src.signals_display.combined_opening", "--journal", str(tmp_path / "live.db"),
                          "--output", str(tmp_path / "combined.json"), "--previous-report", str(previous),
                          "--asof", (NOW + timedelta(days=10)).isoformat()], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert run.returncode == 0, run.stderr[-2000:]
    report = json.loads((tmp_path / "combined.json").read_text())
    assert report["status"] == "MEASURED" and report["fresh"]["contracts"] == 34
    assert (tmp_path / "live.db").read_bytes() == before


def test_live_bot_never_imports_combined_module():
    for path in (ROOT / "src").rglob("*.py"):
        if path.name not in ("combined_opening.py",):
            assert "combined_opening" not in path.read_text(), path
    code = (ROOT / "src/signals_display/combined_opening.py").read_text()
    assert "mode=ro" in code and "PRAGMA query_only=ON" in code
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "model_registry", "place_order", "create_order"):
        assert forbidden not in code


def test_family_value_finds_the_family_that_carries_the_signal(base):
    _, report = base
    fv = report["family_value_development"]
    assert fv["families"]["momentum_acceleration"]["verdict"] == "helps"
    assert fv["families"]["momentum_acceleration"]["delta_log_loss_if_removed"] == max(
        v["delta_log_loss_if_removed"] for v in fv["families"].values())


def test_fresh_report_includes_net_ev_after_costs(base):
    _, report = base
    ev = report["fresh"]["net_ev_after_spread_fees"]["combined_operating"]
    assert ev["calls_with_executable_quote"] == report["fresh"]["combined_operating"]["calls"]
    assert "net EV after spread/fees/slippage" in co.summary(report)

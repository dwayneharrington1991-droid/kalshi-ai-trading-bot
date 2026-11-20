"""Shadow five-specialist architecture. All rows are SYNTHETIC."""
import copy
import json
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import learning, rigorous_synthetic as synthetic, specialist_opening as so
from tests.test_signals_opening_research import _augment

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make(n, seed, start, live=True):
    rows = synthetic.opening_rows(n, seed=seed, signal=0.0, start=start)
    rng = random.Random(seed)
    for r in rows:
        f = r["features"]
        a, b = rng.gauss(0, 1), rng.gauss(0, 1)
        f["return_5m"] = 0.0009 * a
        f["spot_book_imbalance_top3"] = b if live else None
        for name in so.SPECIALIST_FEATURES["pattern"] + ("volatility_24h", "candle_atr_zscore_20"):
            f[name] = rng.gauss(0, 1)
        r["outcome"] = int(rng.random() < learning.sigmoid(
            learning.logit(r["baseline"]) + 0.9 * a + (0.9 * b if live else 0.0)))
        r["_a"] = a
    return rows


def data():
    retro = make(500, 2, START, live=False)
    for r in retro:
        r.update(recorded_up=None, recorded_model="RETROSPECTIVE_RECONSTRUCTION", quote=None)
    live = _augment(make(260, 3, START + timedelta(days=8)))
    rng = random.Random(9)
    for r in live:
        r["recorded_up"] = learning.sigmoid(learning.logit(r["baseline"]) + 0.7 * r["_a"] + rng.gauss(0, 0.7))
    o = sorted(live, key=lambda r: r["closes_at"])
    return live, retro, o[170]["closes_at"], o[214]["closes_at"]


@pytest.fixture(scope="module")
def base():
    live, retro, hs, fa = data()
    return (live, retro, hs, fa), so.evaluate(copy.deepcopy(live), copy.deepcopy(retro), hs, fa)


def test_every_input_belongs_to_exactly_one_specialist():
    seen = [f for feats in so.SPECIALIST_FEATURES.values() for f in feats]
    assert len(seen) == len(set(seen))
    assert set(so.SPECIALISTS) == {"trend", "flow", "pattern", "regime"}


def test_specialist_value_finds_planted_signals_and_flags_the_rest(base):
    _, report = base
    v = report["specialist_value"]
    assert v["trend"]["verdict"] == "adds value" and v["flow"]["verdict"] == "adds value"
    assert v["pattern"]["verdict"].startswith("no measurable value")
    assert v["regime"]["verdict"].startswith("no measurable value")


def test_fresh_labels_never_change_development_choices(base):
    (live, retro, hs, fa), report = base
    flipped = copy.deepcopy(live)
    for r in flipped:
        if r["closes_at"] > fa:
            r["outcome"] = 1 - r["outcome"]
    other = so.evaluate(flipped, copy.deepcopy(retro), hs, fa)
    for key in ("selected", "development_log_loss", "specialist_value", "operating_threshold", "confidence_gate"):
        assert json.dumps(other[key], sort_keys=True, default=str) == json.dumps(report[key], sort_keys=True, default=str)


def test_master_and_gate_learn_only_from_earlier_rows(base):
    (live, retro, hs, fa), _ = base
    rows = sorted(copy.deepcopy(live), key=lambda r: r["closes_at"])[:170]
    oof = so.out_of_fold(rows, [])
    a, ga = so.prequential(rows, oof)
    rows[-1]["outcome"] = 1 - rows[-1]["outcome"]
    b, gb = so.prequential(rows, oof)
    earlier = [r["ticker"] for r in rows[:-1] if r["ticker"] in a["master_full"]]
    assert all(a["master_full"][t] == b["master_full"][t] for t in earlier)
    assert all(ga["master_full"].get(t) == gb["master_full"].get(t) for t in earlier)


def test_report_compares_every_architecture_on_identical_fresh_contracts(base):
    (live, retro, hs, fa), report = base
    f = report["fresh"]
    assert set(f["every_market"]) == set(so.CANDIDATES)
    assert {m["markets"] for m in f["every_market"].values()} == {f["contracts"]}
    assert f["decision_latency_ms"]["p99"] < 50
    assert not any(r["ticker"] in json.dumps(report) for r in retro)
    assert "FRESH UNSEEN CONTRACTS" in so.summary(report)


def test_runner_refuses_to_start_while_another_evaluation_runs(tmp_path):
    root = tmp_path / "research"
    (root / "combined-opening-aaaaaaaaaaaa").mkdir(parents=True)
    (root / "shadow-upgrade-e2357e058e67").mkdir()
    (root / "shadow-upgrade-e2357e058e67" / "report.json").write_text("{}")
    sleeper = subprocess.Popen(["sleep", "60"])
    (root / "combined-opening-aaaaaaaaaaaa" / "pid").write_text(str(sleeper.pid))
    journal = tmp_path / "live.db"
    journal.write_bytes(b"")
    listener = subprocess.Popen([sys.executable, "-c", "import socket,time; s=socket.socket(); "
                                 "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('127.0.0.1', 8768)); "
                                 "s.listen(); time.sleep(60)", "--journal", str(journal)])
    try:
        time.sleep(1)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        run = subprocess.run(["bash", str(ROOT / "deploy/oracle/shadow_upgrade_8768.sh"), sha, "specialist_opening"],
                             cwd=ROOT, capture_output=True, text=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SHADOW_REPO": str(ROOT),
                                  "SHADOW_RUN_ROOT": str(root), "HOME": str(tmp_path)})
        assert run.returncode == 3, run.stdout + run.stderr
        assert "BLOCKED: another shadow evaluation is still running" in run.stdout
        assert not (root / f"specialist-opening-{sha[:12]}").exists()
    finally:
        listener.kill(); sleeper.kill()

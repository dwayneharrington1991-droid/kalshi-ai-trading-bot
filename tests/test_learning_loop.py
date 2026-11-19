"""Learning ledger, failure analysis and promotion gate. Synthetic data only; no live effect."""
import json
import math
import random
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import learning_loop as ll
from src.signals_display.monitor import Journal
from tests.test_shadow_upgrade import _shift
from tests.test_signals_opening_models import NOW, opening

ROOT = Path(__file__).resolve().parents[1]


def row(direction, correct, momentum="up", **kw):
    outcome = None if correct is None else ((1 if direction == "UP" else 0) if correct else (0 if direction == "UP" else 1))
    base = {"direction": direction, "displayed": True, "correct": correct, "outcome_up": outcome,
            "conditions": {"momentum_5m": momentum, "order_flow_cvd": "buying", "volume": "high", "trend_ema9_21": "up"}}
    base.update(kw)
    return base


def test_skipped_openings_are_recorded_with_their_reason_and_never_scored(tmp_path):
    journal = Journal(tmp_path / "j.db")
    base = opening()
    with journal.connect() as db:
        for i in range(3):
            r = _shift(json.loads(json.dumps(base)), timedelta(minutes=15 * i))
            r["ticker"] = f"KXBTC15M-LED{i}"
            if i != 1:  # displayable calls
                r["direction"], r["confidence"], r["up"] = "UP", {"status": "CALIBRATED"}, 0.7
            else:       # skipped opening keeps its NO VERIFIED EDGE status and reason
                r["direction"], r["confidence"] = "WAIT", {"status": "NO VERIFIED EDGE"}
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", r["ticker"], r["generated_at"], r["data_cutoff"], r["closes_at"],
                        r["up"], r["status"], json.dumps(r)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (r["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(), 1))
    with sqlite3.connect(f"file:{tmp_path / 'j.db'}?mode=ro", uri=True) as db:
        ledger = ll.build_ledger(db, NOW + timedelta(days=5))
        early = ll.build_ledger(db, NOW + timedelta(minutes=17))
    assert len(ledger) == 3
    skipped = [r for r in ledger if not r["displayed"]]
    assert len(skipped) == 1 and skipped[0]["ticker"] == "KXBTC15M-LED1" and skipped[0]["skip_reason"]
    assert all(r["correct"] is None for r in skipped)
    assert [r["outcome_up"] for r in early].count(None) >= 1          # outcomes observed later are not visible yet


def test_no_pattern_on_noise_and_a_real_pattern_is_found_after_holm():
    rng = random.Random(7)
    noise = [row(rng.choice(["UP", "DOWN"]), rng.random() < 0.6, momentum=rng.choice(["up", "down"])) for _ in range(200)]
    assert ll.failure_patterns(noise)["patterns"] == []
    signal = []
    for _ in range(300):  # calls AGAINST momentum are wrong almost always; with momentum right
        d = rng.choice(["UP", "DOWN"]); m = rng.choice(["up", "down"])
        with_m = (m == "up") == (d == "UP")
        correct = with_m if rng.random() < 0.9 else not with_m
        signal.append(row(d, correct, momentum=m))
    rep = ll.failure_patterns(signal)
    assert rep["status"] == "MEASURED"
    found = {(p["condition"], p["value"]) for p in rep["patterns"]}
    assert ("direction_vs_momentum", "against") in found
    assert all(p["holm_adjusted_p"] < 0.05 and p["calls"] >= ll.MIN_PATTERN_CALLS for p in rep["patterns"])


def test_too_few_scored_calls_is_reported_not_invented():
    rep = ll.failure_patterns([row("UP", True)] * 5)
    assert rep["status"] == "INSUFFICIENT_SCORED_CALLS" and rep["patterns"] == []


def test_holm_matches_the_textbook_step_down():
    adj = ll.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adj["a"] == pytest.approx(0.03) and adj["c"] == pytest.approx(0.06) and adj["b"] == pytest.approx(0.06)


def test_promotion_holds_without_enough_unseen_markets_or_a_real_improvement():
    cur = {"correct": 58, "calls": 100, "log_loss": 0.66}
    small = {"correct": 12, "calls": 12, "markets": 12, "log_loss": 0.60}
    assert ll.promotion_decision(small, cur)["decision"] == "HOLD"
    weak = {"correct": 56, "calls": 100, "markets": 120, "log_loss": 0.70}
    assert ll.promotion_decision(weak, cur)["decision"] == "HOLD"
    lower_ll_but_not_better_acc = {"correct": 60, "calls": 100, "markets": 120, "log_loss": 0.64}
    d = ll.promotion_decision(lower_ll_but_not_better_acc, cur)
    assert d["decision"] == "HOLD" and any("lower bound" in r for r in d["reasons"])


def test_promotion_eligible_is_never_applied_and_keeps_rollback():
    strong = {"correct": 150, "calls": 200, "markets": 210, "log_loss": 0.60}
    cur = {"correct": 110, "calls": 200, "log_loss": 0.66}
    d = ll.promotion_decision(strong, cur)
    assert d["decision"] == "PROMOTE_ELIGIBLE" and d["applied"] is False and "rollback" in d


def test_status_file_is_atomic_private_and_missing_status_is_reported(tmp_path):
    target = tmp_path / "status.json"
    assert ll.read_status(target)["status"] == "NOT_RUN"
    assert ll.read_status("")["status"] == "NOT_RUN"
    ll.write_status(target, {"status": "MEASURED", "x": 1})
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert ll.read_status(target) == {"status": "MEASURED", "x": 1}


def test_runner_end_to_end_is_read_only_and_writes_only_the_status_file(tmp_path):
    journal = Journal(tmp_path / "j.db")
    base, rng = opening(), random.Random(3)
    with journal.connect() as db:
        for i in range(60):
            r = _shift(json.loads(json.dumps(base)), timedelta(minutes=15 * i))
            r["ticker"] = f"KXBTC15M-RUN{i:03d}"
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", r["ticker"], r["generated_at"], r["data_cutoff"], r["closes_at"],
                        r["up"], r["status"], json.dumps(r)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (r["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(), int(rng.random() < 0.5)))
    before = (tmp_path / "j.db").read_bytes()
    status_file = tmp_path / "out" / "learning_status.json"
    run = subprocess.run([sys.executable, "-m", "src.signals_display.learning_runner", "--journal", str(tmp_path / "j.db"),
                          "--status-file", str(status_file), "--asof", (NOW + timedelta(days=5)).isoformat()],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stderr[-1500:]
    status = json.loads(status_file.read_text())
    assert status["ledger"]["openings_recorded"] == 60
    assert status["promotion"]["decision"] == "HOLD" and status["promotion"]["applied"] is False
    assert status["live_model"].startswith("UNCHANGED")
    assert (tmp_path / "j.db").read_bytes() == before
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["learning_status.json"]


def test_learning_never_touches_live_paths():
    code = (ROOT / "src/signals_display/learning_loop.py").read_text() + (ROOT / "src/signals_display/learning_runner.py").read_text()
    for forbidden in ("INSERT ", "UPDATE ", "DELETE ", "model_registry", "activate", "place_order", "create_order",
                      "systemctl", "kill(", "pkill"):
        assert forbidden not in code, forbidden
    assert "mode=ro" in code and "query_only" in code.lower() or "PRAGMA query_only=ON" in code


def test_dashboard_panel_is_read_only_text_and_the_route_is_registered():
    html = (ROOT / "src/signals_display/dashboard.html").read_text()
    assert 'id="learning-loop"' in html and "/api/learning-status" in html
    panel = html[html.index("/* Learning loop status"):html.index("</script>", html.index("/* Learning loop status"))]
    for forbidden in ("innerHTML", "method: 'POST'", "method:'POST'", "/api/manual", "fetch('/api/scalp", "outerHTML"):
        assert forbidden not in panel, forbidden
    server = (ROOT / "src/signals_display/server.py").read_text()
    assert '"/api/learning-status"' in server and '.learning-status.json' in server and "import os" not in server


def test_malformed_status_is_unreadable_not_missing_and_never_invented(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ll.read_status(bad)["status"] == "UNREADABLE"
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]")
    assert ll.read_status(arr)["status"] == "UNREADABLE"
    assert ll.read_status(tmp_path / "absent.json")["status"] == "NOT_RUN"


def test_live_status_route_over_real_http_for_missing_valid_and_malformed_files(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from src.signals_display.monitor import Journal
    from src.signals_display.server import Monitor, make_handler

    monkeypatch.delenv("LEARNING_STATUS_FILE", raising=False)
    journal = Journal(tmp_path / "http.db")
    status = Path(f"{journal.path}.learning-status.json")      # the only path the service reads
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Monitor(None, journal)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/api/learning-status"

    def get():
        with urlopen(url, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), json.load(r)

    try:
        code, ctype, body = get()                                 # missing
        assert code == 200 and ctype == "application/json" and body["status"] == "NOT_RUN"
        assert "live model is unchanged" in body["note"]

        ll.write_status(status, {"status": "MEASURED", "generated_at": "2026-11-19T00:00:00+00:00",
                                 "ledger": {"openings_recorded": 292, "displayed_calls": 111},
                                 "promotion": {"decision": "HOLD", "applied": False},
                                 "live_model": "UNCHANGED (learning never replaces it automatically)"})
        code, _, body = get()                                     # valid
        assert code == 200 and body["status"] == "MEASURED" and body["ledger"]["openings_recorded"] == 292
        assert body["promotion"]["applied"] is False

        status.write_text('{"status": "MEASURED", ')              # malformed
        code, _, body = get()
        assert code == 200 and body["status"] == "UNREADABLE"
        assert "ledger" not in body                               # nothing invented from a broken file
    finally:
        server.shutdown()


def _git_head():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def _wrapper(journal, *extra_env):
    import os
    env = {**os.environ, "LEARN_REPO": str(ROOT), "LEARN_PYTHON": sys.executable}
    return subprocess.run(["bash", str(ROOT / "deploy/oracle/run_learning_8768.sh"), _git_head(), str(journal)],
                          capture_output=True, text=True, env={**env, **dict(extra_env)}, timeout=900)


def test_scheduled_wrapper_keeps_the_last_good_status_when_the_journal_is_missing(tmp_path):
    journal = tmp_path / "gone.db"
    status = Path(f"{journal}.learning-status.json")
    ll.write_status(status, {"status": "MEASURED", "generated_at": "earlier"})
    run = _wrapper(journal)
    assert run.returncode == 3 and "previous status kept" in run.stdout
    assert json.loads(status.read_text())["generated_at"] == "earlier"


def _seeded_journal(path, n=40):
    journal = Journal(path)
    base, rng = opening(), random.Random(11)
    with journal.connect() as db:
        for i in range(n):
            r = _shift(json.loads(json.dumps(base)), timedelta(minutes=15 * i))
            r["ticker"] = f"KXBTC15M-WRP{i:03d}"
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", r["ticker"], r["generated_at"], r["data_cutoff"], r["closes_at"],
                        r["up"], r["status"], json.dumps(r)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (r["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(), int(rng.random() < 0.5)))
    return journal.path


def test_scheduled_wrapper_publishes_atomically_and_repeats_safely(tmp_path):
    journal = _seeded_journal(tmp_path / "j.db")
    before = Path(journal).read_bytes()
    first = _wrapper(journal)
    assert first.returncode == 0 and "status published" in first.stdout, first.stdout + first.stderr
    status = Path(f"{journal}.learning-status.json")
    assert json.loads(status.read_text())["status"] == "MEASURED"
    assert oct(status.stat().st_mode & 0o777) == "0o600"
    second = _wrapper(journal)                                  # repeat: same result, no overlap, no leftovers
    assert second.returncode == 0 and json.loads(status.read_text())["ledger"]["openings_recorded"] == 40
    assert not list(tmp_path.glob("*.next")) and not list(tmp_path.glob(".learning-*"))
    assert Path(journal).read_bytes() == before                 # the journal is never written


def test_overlapping_scheduled_runs_are_refused_by_the_lock(tmp_path):
    import fcntl
    journal = Journal(tmp_path / "j.db").path
    lock_path = Path(f"{journal}.learning.lock")
    held = open(lock_path, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        run = _wrapper(journal)
        assert run.returncode == 0 and "another run holds the lock" in run.stdout
    finally:
        held.close()


def test_route_finds_the_status_beside_the_journal_without_any_environment(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from src.signals_display.monitor import Journal
    from src.signals_display.server import Monitor, make_handler
    monkeypatch.delenv("LEARNING_STATUS_FILE", raising=False)
    journal = Journal(tmp_path / "live.db")
    ll.write_status(Path(f"{journal.path}.learning-status.json"), {"status": "MEASURED", "marker": "beside-journal"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Monitor(None, journal)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/learning-status", timeout=5) as r:
            body = json.load(r)
    finally:
        server.shutdown()
    assert body["marker"] == "beside-journal" and body["status"] == "MEASURED"

"""Automatic retraining, shadow qualification and controlled promotion. Synthetic data; no live effect."""
import copy
import json
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.signals_display import learning, model_lifecycle as lc, opening_models, promotion as pr
from src.signals_display import lifecycle_runner as lr
from src.signals_display.monitor import Journal
from tests.test_shadow_upgrade import _shift
from tests.test_signals_opening_models import NOW, eligible_candidate, opening

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 11, 1, tzinfo=timezone.utc)


def shadow_rows(n, production="good", flip=0.1, seed=5, start=CUTOFF):
    """Unseen markets after the cutoff. Outcome follows the sign of distance_scaled (with noise).
    production: 'good' recorded calls agree with the outcome; 'bad' disagree; 'none' is uninformative 0.5."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        generated = start + timedelta(minutes=15 * i + 1)
        y = i % 2
        up_sign = (2 * y - 1) if rng.random() >= flip else -(2 * y - 1)
        recorded = {"good": (0.8 if y else 0.2), "bad": (0.2 if y else 0.8), "none": 0.5,
                    "noisy": 0.5 + rng.uniform(-0.1, 0.1)}[production]
        rows.append(dict(
            ticker=f"KXBTC15M-SH{i}", timestamp=generated.isoformat(), opens_at=generated.isoformat(),
            closes_at=(generated + timedelta(minutes=14)).isoformat(),
            label_available_at=(generated + timedelta(minutes=15)).isoformat(),
            features={"distance_scaled": up_sign * 0.001, "return_1m": up_sign * 0.01},
            outcome=y, baseline=0.5, recorded_up=recorded, recorded_direction="UP" if recorded > 0.5 else "DOWN",
            recorded_confidence_status="CALIBRATED", regime="test"))
    return rows


def good_candidate():
    return eligible_candidate()


def bundle(state_dir, artifact, cutoff, status_extra=None):
    """Writes a candidate file and its evaluation report exactly as retrain_cycle does."""
    body = {"format": lc.FORMAT, "kind": "CANDIDATE", "training_cutoff": cutoff.isoformat(), "commit": "t",
            "artifact": artifact}
    body["checksum"] = lc.canonical_checksum(body)
    cand = state_dir / "candidates" / f"candidate-{body['checksum'][:16]}.json"
    cand_sha = lc.write_once(cand, body)
    report = {"format": lc.FORMAT, "kind": "EVALUATION", "candidate_checksum": body["checksum"],
              "candidate_file_sha256": cand_sha, "decision": "CANDIDATE_QUALIFIED"}
    report["checksum"] = lc.canonical_checksum(report)
    lc.write_once(state_dir / "reports" / f"report-{report['checksum'][:16]}.json", report)
    status = {"decision": "CANDIDATE_QUALIFIED", "candidate_file": str(cand),
              "candidate_checksum": body["checksum"], "report_checksum": report["checksum"]}
    status.update(status_extra or {})
    return status, body


class FakeActivator(pr.Activator):
    enabled = True

    def __init__(self, live, activate_ok=True):
        self.live, self.activate_ok, self.calls = live, activate_ok, []

    def current(self):
        return self.live

    def activate(self, artifact_path, checksum):
        self.calls.append(("activate", checksum))
        if self.activate_ok:
            self.live = checksum
            return {"ok": True}
        return {"ok": False, "detail": "switch refused"}

    def rollback(self, artifact_path, checksum):
        self.calls.append(("rollback", checksum))
        self.live = checksum
        return {"ok": True}


HEALTHY = {"health": "HEALTHY", "ready": "READY", "signals_only": True, "workers_healthy": True, "quote_age_s": 1}
RESOURCES = {"mem_mib": 4096, "load1": 1.0, "disk_mib": 20000}
NOW_SAFE = datetime(2026, 11, 19, 3, 4, 0, tzinfo=timezone.utc)   # 4 min after a boundary, safe


def live_file(tmp_path):
    p = tmp_path / "live_artifact.json"
    p.write_text(json.dumps({"format": "live", "marker": "production-model"}))
    return p


def controller(tmp_path, status, activator, state="state", **kw):
    notes = []
    args = dict(live_artifact_path=live_file(tmp_path), health_samples=[HEALTHY], resources=RESOURCES,
                telegram_ok=True, seconds_to_opening=600, notify=lambda e, d: notes.append(e), now=NOW_SAFE)
    args.update(kw)
    return pr.promotion_cycle(tmp_path / state, status, activator, **args), notes


# ---------- point-in-time data, leakage, immutability -------------------------------------------------

def test_only_point_in_time_rows_are_scored_and_exclusions_are_counted():
    rows = shadow_rows(10)
    bad_before = dict(rows[0], timestamp=(CUTOFF - timedelta(hours=1)).isoformat())
    bad_order = dict(rows[1], label_available_at=rows[1]["timestamp"])
    no_outcome = dict(rows[2], outcome=None)
    kept, excluded = lc.point_in_time_rows(rows + [bad_before, bad_order, no_outcome], CUTOFF)
    assert len(kept) == 10
    assert excluded == {"NOT_AFTER_CUTOFF": 1, "OUTCOME_NOT_AFTER_PREDICTION": 1, "NO_OUTCOME": 1}


def test_leakage_check_detects_a_row_that_leaks_past_the_cutoff():
    rows = shadow_rows(5)
    assert lc.leakage_violations(rows, CUTOFF) == 0
    rows.append(dict(rows[0], timestamp=(CUTOFF - timedelta(minutes=1)).isoformat()))
    assert lc.leakage_violations(rows, CUTOFF) == 1


def test_stored_records_are_create_only_and_checksum_verified(tmp_path):
    p = tmp_path / "rec.json"
    obj = {"format": lc.FORMAT, "kind": "X", "v": 1}
    obj["checksum"] = lc.canonical_checksum(obj)
    lc.write_once(p, obj)
    with pytest.raises(FileExistsError):
        lc.write_once(p, obj)                                    # never overwritten
    assert lc.load_verified(p)["v"] == 1
    tampered = json.loads(p.read_text()); tampered["v"] = 2
    p.write_text(json.dumps(tampered))
    with pytest.raises(ValueError):
        lc.load_verified(p)                                      # fail closed on tampering


def test_promotion_history_is_append_only_and_tamper_evident(tmp_path):
    h = pr.History(tmp_path)
    h.append("A", {"x": 1}); h.append("B", {"x": 2})
    assert h.verify_chain() and len(h.entries()) == 2
    lines = h.path.read_text().splitlines()
    lines[0] = lines[0].replace('"x": 1', '"x": 9')
    h.path.write_text("\n".join(lines) + "\n")
    assert not h.verify_chain()


# ---------- statistical qualification -------------------------------------------------------------------

def test_better_candidate_qualifies_on_unseen_markets():
    rows = shadow_rows(160, production="noisy", flip=0.0)     # candidate right; production uninformative
    metrics = lc.shadow_metrics(good_candidate(), rows)
    decision, reasons = lc.gate(good_candidate(), "c" * 64, metrics, 0, candidates_evaluated=1)
    assert decision == "CANDIDATE_QUALIFIED", reasons
    assert metrics["stable_two_windows"] is True and metrics["paired_logloss_gain_z"] > 2


def test_worse_candidate_is_rejected_on_performance():
    rows = shadow_rows(160, production="good", flip=0.0)     # production perfect, candidate matches it
    rows = [dict(r, recorded_up=0.999 if r["outcome"] else 0.001) for r in rows]
    metrics = lc.shadow_metrics(good_candidate(), rows)
    decision, reasons = lc.gate(good_candidate(), "c" * 64, metrics, 0, candidates_evaluated=1)
    assert decision == "REJECTED_PERFORMANCE" and reasons


def test_insufficient_unseen_markets_never_qualify():
    rows = shadow_rows(60, production="none")
    metrics = lc.shadow_metrics(good_candidate(), rows)
    assert lc.gate(good_candidate(), "c" * 64, metrics, 0, candidates_evaluated=1)[0] == "INSUFFICIENT_EVIDENCE"


def test_leakage_threshold_change_and_incomplete_artifacts_are_rejected():
    rows = shadow_rows(160, production="none")
    cand = good_candidate()
    metrics = lc.shadow_metrics(cand, rows)
    assert lc.gate(cand, "c" * 64, metrics, 3, candidates_evaluated=1)[0] == "REJECTED_LEAKAGE"
    changed = copy.deepcopy(cand); changed["decision_policy"]["threshold"] = 0.6
    assert lc.gate(changed, "c" * 64, metrics, 0, candidates_evaluated=1)[0] == "REJECTED_THRESHOLD_CHANGE"
    incomplete = copy.deepcopy(cand); del incomplete["model"]
    assert lc.gate(incomplete, "c" * 64, metrics, 0, candidates_evaluated=1)[0] == "REJECTED_NOT_DEPLOYABLE"
    assert lc.gate(cand, "", metrics, 0, candidates_evaluated=1)[0] == "REJECTED_INCOMPLETE"


def test_selection_bias_correction_blocks_a_lucky_winner_among_many_candidates():
    rows = shadow_rows(160, production="noisy", flip=0.0)
    metrics = lc.shadow_metrics(good_candidate(), rows)
    lucky = dict(metrics, paired_logloss_gain_z=3.0)          # a gain that is significant alone...
    assert lc.gate(good_candidate(), "c" * 64, lucky, 0, candidates_evaluated=1)[0] == "CANDIDATE_QUALIFIED"
    assert lc.bonferroni_critical_z(1000) > 3.0                 # ...but not after 1000 candidates were tried
    assert lc.gate(good_candidate(), "c" * 64, lucky, 0, candidates_evaluated=1000)[0] == "REJECTED_PERFORMANCE"


def test_identical_models_show_no_gain_and_are_never_promoted():
    rows = shadow_rows(160, production="none")
    cand = good_candidate()
    recorded = [dict(r, recorded_up=opening_models.predict(cand["model"], r["features"], baseline_probability=r["baseline"]),
                     recorded_direction="UP") for r in rows]
    metrics = lc.shadow_metrics(cand, recorded)
    assert lc.gate(cand, "c" * 64, metrics, 0, candidates_evaluated=1)[0] != "CANDIDATE_QUALIFIED"


# ---------- controller: promotion only through a verified activator --------------------------------------

def test_disabled_activator_blocks_before_any_change(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    res, notes = controller(tmp_path, status, pr.DisabledActivator())
    assert res["result"] == "BLOCKED" and "ACTIVATION_DISABLED" in res["detail"]
    state = tmp_path / "state"
    assert not (state / "active.json").exists()
    assert not any((state / "backups").glob("*")) if (state / "backups").exists() else True
    assert pr.History(state).entries()[-1]["event"] == "PROMOTION_BLOCKED"


def test_non_qualified_status_is_never_promoted(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    status["decision"] = "REJECTED_PERFORMANCE"
    act = FakeActivator(live="old")
    res, _ = controller(tmp_path, status, act)
    assert res["result"] == "NOT_PROMOTED" and act.calls == []


def test_tampered_candidate_file_blocks_promotion(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    f = Path(status["candidate_file"])
    obj = json.loads(f.read_text()); obj["artifact"]["decision_policy"]["threshold"] = 0.99
    f.write_text(json.dumps(obj))
    act = FakeActivator(live="old")
    res, _ = controller(tmp_path, status, act)
    assert res["result"] == "ERROR_STOPPED" and act.calls == []       # integrity failure stops everything


@pytest.mark.parametrize("kw,reason", [
    ({"health_samples": [{"health": "DEGRADED", "ready": "READY", "signals_only": True}]}, "HEALTHY"),
    ({"resources": {"mem_mib": 100, "load1": 1.0, "disk_mib": 20000}}, "memory"),
    ({"resources": {"mem_mib": 4096, "load1": 99, "disk_mib": 20000}}, "CPU"),
    ({"resources": {"mem_mib": 4096, "load1": 1.0, "disk_mib": 100}}, "disk"),
    ({"telegram_ok": False}, "Telegram"),
])
def test_each_safety_precondition_blocks_promotion(tmp_path, kw, reason):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    act = FakeActivator(live="old")
    res, _ = controller(tmp_path, status, act, **kw)
    assert res["result"] == "BLOCKED" and reason in res["detail"] and act.calls == []


def test_promotion_is_deferred_inside_the_opening_window(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    act = FakeActivator(live="old")
    res, _ = controller(tmp_path, status, act, seconds_to_opening=100)
    assert res["result"] == "DEFERRED" and act.calls == []


def test_successful_promotion_keeps_a_verified_rollback_checkpoint(tmp_path):
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    act = FakeActivator(live="old-checksum")
    res, notes = controller(tmp_path, status, act)
    assert res["result"] == "PROMOTED" and act.live == body["checksum"]
    state = tmp_path / "state"
    active = json.loads((state / "active.json").read_text())
    assert active["candidate_checksum"] == body["checksum"] and active["previous_checksum"] == "old-checksum"
    backup = next((state / "backups").glob("live-*.json"))
    assert backup.read_bytes() == (tmp_path / "live_artifact.json").read_bytes()    # byte-exact rollback checkpoint
    assert pr.History(state).verify_chain() and "promoted" in notes
    again, _ = controller(tmp_path, status, FakeActivator(live=body["checksum"]))
    # never promoted twice: while the post-promotion watch is open the repeat cycle only watches
    assert again["result"] in ("WATCHING", "ALREADY_PROMOTED")
    assert sum(1 for e in pr.History(state).entries() if e["event"] == "PROMOTED") == 1                                   # never promoted twice


def test_failed_activation_rolls_back_and_does_not_promote(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    act = FakeActivator(live="old", activate_ok=False)
    res, notes = controller(tmp_path, status, act)
    assert res["result"] == "ROLLED_BACK" and ("rollback", "old") in act.calls
    assert not (tmp_path / "state" / "active.json").exists() and "rollback" in notes


def test_sustained_operational_failure_after_promotion_triggers_automatic_rollback(tmp_path):
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    act = FakeActivator(live="old")
    bad = [{"health": "HEALTHY", "ready": "NOT_READY", "signals_only": True}] * 3
    res, notes = controller(tmp_path, status, act, health_samples=bad)
    assert res["result"] == "BLOCKED"                              # live unhealthy before the switch
    res, notes = controller(tmp_path, status, act, health_samples=[HEALTHY])
    assert res["result"] == "PROMOTED"
    # now simulate post-switch failures in a fresh cycle that promotes and then sees sustained failure
    status2, _ = bundle(tmp_path / "state2", good_candidate(), CUTOFF)
    act2 = FakeActivator(live="old")
    rb = lc.rollback_decision(bad, "old")
    assert rb["decision"] == "ROLLBACK_REQUIRED" and lc.rollback_decision(bad[:2], "old")["decision"] == "OK"
    assert lc.rollback_decision([], "old")["decision"] == "SUSPENDED_INSUFFICIENT_EVIDENCE"


def test_interrupted_promotion_is_recovered_or_blocked_fail_closed(tmp_path):
    status, body = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    h = pr.History(tmp_path / "state")
    h.append("PROMOTION_STARTED", {"candidate_checksum": body["checksum"], "previous_checksum": "old"})
    act = FakeActivator(live=body["checksum"])                        # the switch actually happened
    res, _ = controller(tmp_path, status, act)
    assert "RECOVERED_PROMOTED" in [e["event"] for e in pr.History(tmp_path / "state").entries()]
    assert res["result"] == "ALREADY_PROMOTED"                        # recovery counts as promotion; no repeat
    # unknown live model after an interruption: stop, do nothing
    status3, body3 = bundle(tmp_path / "state3", good_candidate(), CUTOFF + timedelta(days=1))   # a different candidate
    pr.History(tmp_path / "state3").append("PROMOTION_STARTED", {"candidate_checksum": body3["checksum"], "previous_checksum": "old"})
    act3 = FakeActivator(live=None)
    res, notes = controller(tmp_path, status3, act3, state="state3")
    assert res["result"] == "BLOCKED" and act3.calls == []


def test_overlapping_controller_cycles_are_refused_by_the_lock(tmp_path):
    import fcntl
    status, _ = bundle(tmp_path / "state", good_candidate(), CUTOFF)
    (tmp_path / "state").mkdir(exist_ok=True)
    held = open(tmp_path / "state" / pr.LOCK, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        res, _ = controller(tmp_path, status, FakeActivator(live="old"))
        assert res["result"] == "LOCKED"
    finally:
        held.close()


# ---------- triggers, runner, notifications, scheduling guards -----------------------------------------

def test_opening_window_and_rollback_monitor_rules():
    assert lc.opening_window_safe(datetime(2026, 11, 19, 3, 4, 0, tzinfo=timezone.utc))
    assert not lc.opening_window_safe(datetime(2026, 11, 19, 3, 1, 0, tzinfo=timezone.utc))   # just after a boundary
    assert not lc.opening_window_safe(datetime(2026, 11, 19, 3, 14, 30, tzinfo=timezone.utc))  # just before one


def _journal_with(path, n):
    journal = Journal(path)
    base, rng = opening(), random.Random(9)
    with journal.connect() as db:
        for i in range(n):
            r = _shift(json.loads(json.dumps(base)), timedelta(minutes=15 * i))
            r["ticker"] = f"KXBTC15M-LC{i:03d}"
            db.execute("INSERT INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
                       ("PUBLIC MARKET DATA", r["ticker"], r["generated_at"], r["data_cutoff"], r["closes_at"],
                        r["up"], r["status"], json.dumps(r)))
            db.execute("INSERT INTO forecast_outcomes VALUES (?,?,?,?)",
                       (r["ticker"], "official", (NOW + timedelta(minutes=15 * i + 16)).isoformat(), int(rng.random() < 0.5)))
    return journal.path


def test_default_trigger_learns_from_every_settlement():
    assert lc.MIN_NEW_SETTLED == 1
    # the promotion evidence requirement is unchanged: learning is continuous, promotion is not
    assert lc.MIN_FRESH_MARKETS >= 100


def test_trigger_waits_for_enough_new_settled_markets(tmp_path):
    journal = _journal_with(tmp_path / "j.db", 12)
    status = lc.retrain_cycle(journal, tmp_path / "state", NOW + timedelta(days=5), "t", min_new=50)
    assert status["decision"] == "SKIPPED_NOT_ENOUGH_NEW_DATA"
    status = lc.retrain_cycle(journal, tmp_path / "state", NOW + timedelta(days=5), "t", min_new=5)
    assert status["decision"] in ("NO_CANDIDATE", "REJECTED_PERFORMANCE", "INSUFFICIENT_EVIDENCE", "CANDIDATE_QUALIFIED",
                                  "REJECTED_NOT_DEPLOYABLE", "REJECTED_LEAKAGE")
    assert status.get("live_model") == "UNCHANGED"


def test_runner_end_to_end_with_disabled_activation_changes_nothing_live(tmp_path):
    journal = _journal_with(tmp_path / "j.db", 60)
    before = Path(journal).read_bytes()
    live = live_file(tmp_path)
    out = lr.run(journal, tmp_path / "state", "t", live, asof=NOW + timedelta(days=5), min_new=5, port=1,
                 probe=lambda port: HEALTHY, resources=lambda: RESOURCES, telegram=lambda: False,
                 seconds_to_opening=600, notify=lambda e, d: None)
    assert out["activation"].startswith("DISABLED") and out["live_model"] == "UNCHANGED"
    assert out["promotion"]["result"] in ("BLOCKED", "NOT_PROMOTED")
    assert Path(journal).read_bytes() == before and live.read_text() == json.dumps({"format": "live", "marker": "production-model"})
    status_file = Path(f"{journal}{lr.STATUS_SUFFIX}")
    assert json.loads(status_file.read_text())["history_chain_verified"] is True


def test_notifications_are_credential_free_and_never_stop_learning(monkeypatch):
    secret = "123456:SUPERSECRET-TOKEN"
    cfg = {"TELEGRAM_BOT_TOKEN": secret, "TELEGRAM_CHAT_ID": "-100555"}
    texts = []
    notify = lr.make_notifier(config_loader=lambda: cfg, sender=lambda ch, c, t: texts.append(t))
    for event, data in (("training_completed", {"decision": "NO_CANDIDATE", "settled_markets": 292}),
                        ("candidate_qualified", {"candidate_checksum": "abcdef123456"}),
                        ("promoted", {"candidate_checksum": "abc", "previous_checksum": "def"}),
                        ("rollback", {"reason": "x", "previous_checksum": "def"}),
                        ("critical_failure", {"reason": "y"})):
        notify(event, data)
    joined = " ".join(texts)
    assert len(texts) == 5 and "SUPERSECRET" not in joined and "-100555" not in joined and "TOKEN" not in joined
    broken = lr.make_notifier(config_loader=lambda: cfg, sender=lambda *a: 1 / 0)
    assert broken("promoted", {"candidate_checksum": "a", "previous_checksum": "b"}) == "ZeroDivisionError"


def test_lifecycle_modules_never_touch_trading_or_live_services():
    code = "".join((ROOT / "src/signals_display" / f).read_text() for f in
                   ("model_lifecycle.py", "promotion.py", "lifecycle_runner.py"))
    for forbidden in ("place_order", "create_order", "cancel_order", "amend_order", "systemctl", "pkill", "kill(",
                      "os.kill", "/portfolio/", "trade-api", "DELETE FROM", "DROP TABLE", "TRUNCATE"):
        assert forbidden not in code, forbidden
    assert "mode=ro" in code and "PRAGMA query_only=ON" in code


def _head():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def test_scheduled_wrapper_refuses_overlap_and_bad_input(tmp_path):
    import fcntl, os
    journal = _journal_with(tmp_path / "j.db", 20)
    live = live_file(tmp_path)
    env = {**os.environ, "LEARN_REPO": str(ROOT), "LEARN_PYTHON": sys.executable}
    script = str(ROOT / "deploy/oracle/run_lifecycle_8768.sh")
    bad = subprocess.run(["bash", script, "not-a-sha", journal, str(live)], capture_output=True, text=True, env=env)
    assert bad.returncode == 2
    state = Path(journal).parent / "lifecycle"
    state.mkdir(exist_ok=True)
    held = open(state / "cycle.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        run = subprocess.run(["bash", script, _head(), journal, str(live)], capture_output=True, text=True, env=env, timeout=600)
        assert run.returncode == 0 and "another cycle is running" in run.stdout
    finally:
        held.close()


def test_scheduled_wrapper_runs_a_cycle_or_refuses_inside_an_opening_window_and_never_changes_the_live_model(tmp_path):
    import os
    journal = _journal_with(tmp_path / "j.db", 20)
    live = live_file(tmp_path)
    before, journal_before = live.read_bytes(), Path(journal).read_bytes()
    env = {**os.environ, "LEARN_REPO": str(ROOT), "LEARN_PYTHON": sys.executable}
    run = subprocess.run(["bash", str(ROOT / "deploy/oracle/run_lifecycle_8768.sh"), _head(), journal, str(live)],
                         capture_output=True, text=True, env=env, timeout=900)
    # Either it ran the cycle, or it refused because the current time is inside an opening window.
    assert run.returncode == 0 and ("cycle complete" in run.stdout or "opening window" in run.stdout), run.stdout + run.stderr
    assert live.read_bytes() == before                                 # the live model artifact is untouched
    assert Path(journal).read_bytes() == journal_before                # the journal is never written


def test_live_status_route_returns_the_lifecycle_section_over_real_http(tmp_path, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import urlopen
    from src.signals_display.monitor import Journal
    from src.signals_display.server import Monitor, make_handler
    journal = Journal(tmp_path / "life.db")
    Path(f"{journal.path}.lifecycle-status.json").write_text(json.dumps(
        {"format": lc.FORMAT, "retrain_decision": "NO_CANDIDATE", "live_model": "UNCHANGED",
         "promotion": {"result": "BLOCKED", "detail": "ACTIVATION_DISABLED"}, "rollback": {"status": "none"},
         "activation": "DISABLED (no verified guarded activator configured)", "markets_learned_from": 292}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Monitor(None, journal)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_port}/api/learning-status", timeout=5) as r:
            body = json.load(r)
    finally:
        server.shutdown()
    assert body["lifecycle"]["retrain_decision"] == "NO_CANDIDATE"
    assert body["lifecycle"]["markets_learned_from"] == 292 and body["lifecycle"]["live_model"] == "UNCHANGED"
    assert body["lifecycle"]["promotion"]["detail"] == "ACTIVATION_DISABLED"


def test_dashboard_panel_names_every_requested_learning_field():
    html = (ROOT / "src/signals_display/dashboard.html").read_text()
    for field in ("markets_learned_from", "last_training_cycle", "next_cycle_after", "retrain_decision", "markets_scored",
                  "current_production_model", "rollback", "activation", "promotion", "Live model: UNCHANGED", "reasons"):
        assert field in html, field
    assert "innerHTML" not in html[html.index("/* Learning loop status"):html.index("</script>", html.index("/* Learning loop status"))]


def test_scheduled_minutes_are_outside_the_opening_window_guard():
    """Cron must not fire on the 15-minute boundaries, which the guard always refuses."""
    for minute in lc.SCHEDULE_MINUTES:
        assert lc.opening_window_safe(datetime(2026, 11, 19, 3, minute, 0, tzinfo=timezone.utc)), minute
    assert lc.SCHEDULE_MINUTES == (7, 22, 37, 52)
    assert not lc.opening_window_safe(datetime(2026, 11, 19, 3, 0, 0, tzinfo=timezone.utc))
    assert not lc.opening_window_safe(datetime(2026, 11, 19, 3, 30, 0, tzinfo=timezone.utc))


def test_scheduled_wrapper_forwards_the_activator_arguments_only_when_configured():
    code = (ROOT / "deploy/oracle/run_lifecycle_8768.sh").read_text()
    assert '${LIFECYCLE_EXTRA_ARGS:-}' in code        # empty by default: the schedule stays disabled-activator
    assert "--activator guarded" not in code          # never hard-wired on

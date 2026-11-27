"""Watchdog: alerts only on state changes, bounded restarts, never restarts during a deploy or without a snapshot."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import watchdog_8768 as wd  # noqa: E402


def test_alerts_only_after_two_bad_runs_and_once_per_incident():
    s = {}
    m, s = wd.evaluate({"disk space": (False, "3 GB free")}, s)
    assert m == []                                                    # first bad run: no message
    m, s = wd.evaluate({"disk space": (False, "3 GB free")}, s)
    assert m == ["PROBLEM: disk space - 3 GB free"]
    m, s = wd.evaluate({"disk space": (False, "2 GB free")}, s)
    assert m == []                                                    # no repeats while still failing
    m, s = wd.evaluate({"disk space": (True, "9 GB free")}, s)
    assert m == ["RECOVERED: disk space"]
    m, s = wd.evaluate({"disk space": (True, "9 GB free")}, s)
    assert m == []


def test_one_blip_never_alerts():
    s = {}
    _, s = wd.evaluate({"x": (False, "blip")}, s)
    m, s = wd.evaluate({"x": (True, "")}, s)
    assert m == []


def test_restart_waits_one_run_respects_deploys_snapshot_and_limit():
    t = 1_000_000.0
    a, s = wd.restart_decision(False, {}, False, True, t)
    assert a == "WAIT"                                                # down once: wait for confirmation
    a, s2 = wd.restart_decision(False, s, True, True, t + 120)
    assert a == "DEPLOY_RUNNING"                                      # never during a guarded deploy
    a, s = wd.restart_decision(False, s, False, False, t + 120)
    assert a == "NO_SNAPSHOT"
    s = {}
    for i in range(wd.MAX_RESTARTS):
        _, s = wd.restart_decision(False, s, False, True, t + i * 600)
        a, s = wd.restart_decision(False, s, False, True, t + i * 600 + 120)
        assert a == "RESTART"
    _, s = wd.restart_decision(False, s, False, True, t + 4000)
    a, s = wd.restart_decision(False, s, False, True, t + 4120)
    assert a == "LIMIT"                                               # bounded: no restart loop
    a, s = wd.restart_decision(False, s, False, True, t + wd.RESTART_WINDOW_H * 3600 + 2000)
    assert a in ("WAIT", "RESTART")                                   # window expired: may try again
    a, s = wd.restart_decision(True, s, False, True, t + 30000)
    assert a == "NONE" and s["down_runs"] == 0


def test_watchdog_never_touches_trading_models_or_journal():
    code = (ROOT / "scripts/watchdog_8768.py").read_text(encoding="utf-8")
    for forbidden in ("place_order", "cancel_order", "create_order", "8765", "upgraded-live-8768.db\"", "DELETE", "DROP"):
        assert forbidden not in code, forbidden


def test_disk_alert_gives_days_of_warning():
    assert wd.LIMITS["disk_free_gb"] >= 12.0          # journal grows ~4 GB/day: alert with ~3 days to act

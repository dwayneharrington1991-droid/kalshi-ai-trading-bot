"""A promotion never touches the live pointer when the captured launch no longer matches the live process.

Found 2026-11-20 after the 3ce9071 deployment: the lifecycle's restart capture still
names the previous build (--build-commit a269d08, old cwd). A qualified candidate
would have swapped the pointer, had the restart refused, swapped back, had that
restart refused too and reported "RESTORE NOT VERIFIED - manual recovery required"
although the live process was never touched. The activator now checks first.
"""
import json
from datetime import datetime, timezone

from src.signals_display import guarded_activator as ga
from src.signals_display import production_restart as pr_

HEALTHY = {"health": "HEALTHY", "ready": "READY", "signals_only": True, "model_file_sha": "a" * 64}
OLD = ["python", "-m", "server", "--opening-model-artifact", "/m/old.json", "--build-commit", "a269d08"]
NEW = ["python", "-m", "server", "--opening-model-artifact", "/m/old.json", "--build-commit", "3ce9071"]


class FakeRestarter:
    def __init__(self, ready):
        self.ready, self.calls = ready, []

    def can_restart(self):
        return self.ready

    def __call__(self, artifact):
        self.calls.append(artifact)
        return False


def _activator(tmp_path, restarter):
    config = {"restart": "production-process", "port": 8768}
    auth = tmp_path / "auth.txt"
    auth.write_text(ga.AUTH_PREFIX + ga.activator_checksum(config))
    act = ga.GuardedActivator(tmp_path, authorization_file=auth, config=config, restarter=restarter,
                              probe=lambda: HEALTHY, window_safe=lambda now: True, sleep=lambda s: None,
                              clock=lambda: datetime(2026, 11, 20, 4, 52, tzinfo=timezone.utc))
    act._verify_candidate = lambda path, checksum: None
    cand = tmp_path / "cand.json"
    cand.write_text("{}")
    act._write_pointer({"checksum": "production:old", "file_sha": "a" * 64, "artifact": "/m/old.json"})
    return act, cand


def test_stale_capture_refuses_before_the_pointer_is_touched(tmp_path):
    restarter = FakeRestarter(ready=False)
    act, cand = _activator(tmp_path, restarter)
    before = (tmp_path / ga.POINTER).read_text()
    out = act.activate(cand, "candidate:new")
    assert out["ok"] is False and out["detail"].startswith("RESTART_TEMPLATE_STALE")
    assert restarter.calls == []
    assert (tmp_path / ga.POINTER).read_text() == before


def test_matching_capture_still_attempts_the_verified_switch(tmp_path):
    restarter = FakeRestarter(ready=True)
    act, cand = _activator(tmp_path, restarter)
    out = act.activate(cand, "candidate:new")
    assert restarter.calls                                  # the switch was attempted (and restored)
    assert "RESTART_TEMPLATE_STALE" not in out["detail"]
    assert json.loads((tmp_path / ga.POINTER).read_text())["checksum"] == "production:old"


def test_restarter_without_precheck_keeps_the_old_behaviour(tmp_path):
    calls = []
    act, cand = _activator(tmp_path, lambda artifact: calls.append(artifact) or False)
    act.activate(cand, "candidate:new")
    assert calls


def _restarter(monkeypatch, live_argv, pid=4242, systemd=False):
    monkeypatch.setattr(pr_, "_listener_pid", lambda port: pid)
    monkeypatch.setattr(pr_, "_cgroup_is_systemd", lambda p: systemd)
    monkeypatch.setattr(pr_, "_cmdline", lambda p: list(live_argv))
    return pr_.ProductionRestarter({"port": 8768, "argv": OLD, "cwd": "/x"}, "/nonexistent", "/nonexistent")


def test_can_restart_is_true_only_for_the_exact_captured_launch(monkeypatch):
    assert _restarter(monkeypatch, OLD).can_restart() is True
    assert _restarter(monkeypatch, NEW).can_restart() is False            # a deployment changed the launch
    assert _restarter(monkeypatch, OLD, systemd=True).can_restart() is False
    assert _restarter(monkeypatch, OLD, pid=None).can_restart() is False

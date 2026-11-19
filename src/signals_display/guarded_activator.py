"""Guarded model activator for the promotion controller. OFF unless explicitly authorized.

It is the only component that may change the live opening model, and it only does so when ALL hold:

  1 an explicit authorization file exists whose content is exactly
    "AUTHORIZE-AUTO-PROMOTION:<activator checksum>" (written by you, never by code)
  2 the candidate artifact matches its checksum, is deployable, and was QUALIFIED
    (the controller has already verified this; re-checked here)
  3 the live model can be identified, and the live probe is HEALTHY, READY, signals-only
  4 the switch is outside a protected 15-minute opening window
  5 the switch is atomic: the active pointer is replaced with os.replace, never edited in place
  6 the restart is performed by an injected, verified restarter (the guarded deploy path);
    without one, activation refuses (RESTART_PATH_NOT_CONFIGURED)
  7 after restart, health is verified; if not, the previous model is restored and verified

Nothing here places orders, touches the journal, or edits the retired services.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import model_lifecycle as lc
from . import opening_models
from . import promotion as pr

AUTH_PREFIX = "AUTHORIZE-AUTO-PROMOTION:"
POINTER = "live_model_pointer.json"


def activator_checksum(config):
    """Identity of the activator configuration the authorization refers to."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


class GuardedActivator(pr.Activator):
    def __init__(self, state_dir, live_pointer, *, authorization_file, config, restarter=None, probe=None,
                 seconds_to_opening=lambda: 900, window_safe=lc.opening_window_safe, now=None):
        self.state_dir = Path(state_dir)
        self.live_pointer = Path(live_pointer)
        self.authorization_file = Path(authorization_file)
        self.config = config
        self.restarter = restarter            # callable(artifact_path) -> bool, the guarded deploy path
        self.probe = probe                    # callable() -> health sample dict of the live service
        self.window_safe = window_safe
        self.enabled = self._authorized()

    def _authorized(self):
        try:
            text = self.authorization_file.read_text().strip()
        except OSError:
            return False
        return text == AUTH_PREFIX + activator_checksum(self.config)

    def current(self):
        try:
            return json.loads(self.live_pointer.read_text())["checksum"]
        except (OSError, ValueError, KeyError):
            return None

    def _verified_artifact(self, path, checksum):
        body = lc.load_verified(path)
        if body.get("checksum") != checksum:
            raise ValueError("artifact checksum does not match")
        opening_models.validate_artifact(body["artifact"])
        return body

    def _point(self, path, checksum):
        tmp = self.live_pointer.with_suffix(".next")
        tmp.write_text(json.dumps({"checksum": checksum, "artifact": str(path)}, sort_keys=True))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.live_pointer)            # atomic replacement; never a partial pointer

    def activate(self, artifact_path, checksum):
        if not self.enabled:
            return {"ok": False, "detail": "ACTIVATION_DISABLED: not authorized"}
        if self.restarter is None or self.probe is None:
            return {"ok": False, "detail": "RESTART_PATH_NOT_CONFIGURED"}
        try:
            self._verified_artifact(artifact_path, checksum)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return {"ok": False, "detail": f"artifact refused ({type(exc).__name__})"}
        if not self.window_safe(datetime.now(timezone.utc)):
            return {"ok": False, "detail": "inside a protected opening window"}
        sample = self.probe()
        if not pr._live_healthy(sample):
            return {"ok": False, "detail": "live service not HEALTHY/READY/signals-only before switch"}
        previous = self.current()
        self._point(artifact_path, checksum)
        if not self.restarter(artifact_path) or not pr._live_healthy(self.probe()):
            if previous is not None:
                self._restore(previous)
            return {"ok": False, "detail": "post-switch health check failed; previous model restored"}
        return {"ok": True, "detail": f"switched to {checksum[:12]}"}

    def _restore(self, previous_checksum):
        backup = self.state_dir / "backups"
        candidates = sorted(backup.glob("live-*.json")) if backup.exists() else []
        for path in candidates:
            if hashlib.sha256(path.read_bytes()).hexdigest().startswith(previous_checksum[:16]):
                self._point(path, previous_checksum)
                if self.restarter:
                    self.restarter(path)
                return True
        return False

    def rollback(self, artifact_path, checksum):
        if not self.enabled or self.restarter is None:
            return {"ok": False, "detail": "rollback path not configured"}
        self._point(artifact_path, checksum)
        ok = bool(self.restarter(artifact_path)) and pr._live_healthy(self.probe() if self.probe else None)
        return {"ok": ok, "detail": "restored" if ok else "rollback health check failed"}

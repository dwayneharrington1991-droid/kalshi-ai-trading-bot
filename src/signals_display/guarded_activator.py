"""Guarded model activator. OFF unless explicitly authorized AND a restart + probe are configured.

Activation is a pointer swap plus a restart, verified end to end:

  1 authorization: state_dir/promotion-authorization.txt must contain exactly
    "AUTHORIZE-AUTO-PROMOTION:<activator_checksum>" where the checksum covers the
    restart configuration. Code never writes this file.
  2 the candidate is verified: checksum, deployable artifact (validate_artifact).
  3 the window guard: never switch inside a protected 15-minute opening window.
  4 the live service is HEALTHY, READY and signals-only before switching.
  5 the pointer (live_model_pointer.json) is replaced ATOMICALLY (write temp, os.replace).
  6 the service is restarted with the new artifact, and the probe must report the
     SAME model file sha256 the activator wrote. Health alone is not enough.
  7 on any failure the previous pointer is restored, the service is restarted with
     the previous artifact, and that model is verified the same way.

Rollback uses the same verified path. Nothing here places orders, touches the journal,
or edits any service other than the one whose restart is configured.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import model_lifecycle as lc
from . import opening_models
from . import promotion as pr

AUTH_PREFIX = "AUTHORIZE-AUTO-PROMOTION:"
POINTER = "live_model_pointer.json"
WAIT_SECONDS = 20.0
WAIT_STEP = 0.25


def activator_checksum(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GuardedActivator(pr.Activator):
    def __init__(self, state_dir, *, authorization_file, config, restarter=None, probe=None,
                 window_safe=lc.opening_window_safe, clock=lambda: datetime.now(timezone.utc),
                 sleep=time.sleep):
        self.state_dir = Path(state_dir)
        self.pointer = self.state_dir / POINTER
        self.authorization_file = Path(authorization_file)
        self.config = config
        self.restarter = restarter            # callable(artifact_path) -> bool
        self.probe = probe                    # callable() -> dict with health fields and model_file_sha
        self.window_safe = window_safe
        self.clock = clock
        self.sleep = sleep
        self.enabled = bool(self._authorized() and restarter is not None and probe is not None)

    def _authorized(self):
        try:
            return self.authorization_file.read_text().strip() == AUTH_PREFIX + activator_checksum(self.config)
        except OSError:
            return False

    # --- pointer -----------------------------------------------------------------------------
    def _read_pointer(self):
        try:
            return json.loads(self.pointer.read_text())
        except (OSError, ValueError):
            return None

    def _write_pointer(self, record):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.pointer.with_name(self.pointer.name + ".next")
        tmp.write_text(json.dumps(record, sort_keys=True))
        os.chmod(tmp, 0o600)
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, self.pointer)          # atomic: readers see the old or the new record, never a partial one

    def bootstrap(self, live_artifact_path):
        """Record the CURRENT production model's identity without changing what is served."""
        if self._read_pointer() is None:
            sha = file_sha(live_artifact_path)
            self._write_pointer({"checksum": "production:" + sha, "file_sha": sha,
                                 "artifact": str(live_artifact_path)})
        return self._read_pointer()

    def current(self):
        record = self._read_pointer()
        return record["checksum"] if record else None

    # --- verification ------------------------------------------------------------------------
    def _verify_candidate(self, artifact_path, checksum):
        body = lc.load_verified(artifact_path)
        if body.get("checksum") != checksum:
            raise ValueError("candidate checksum does not match")
        opening_models.validate_artifact(body["artifact"])

    def _await_model(self, expected_file_sha):
        """Poll the probe until the service reports the expected model and is healthy."""
        deadline = time.monotonic() + WAIT_SECONDS
        last = None
        while True:
            last = self.probe()
            if pr._live_healthy(last) and last.get("model_file_sha") == expected_file_sha:
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(WAIT_STEP)

    def _switch_to(self, record):
        """Point at a model, restart, and verify. Returns True only if the service runs exactly that file."""
        self._write_pointer(record)
        if not self.restarter(Path(record["artifact"])):
            return False
        return self._await_model(record["file_sha"])

    # --- public interface used by the controller ---------------------------------------------
    def activate(self, artifact_path, checksum):
        if not self.enabled:
            return {"ok": False, "detail": "ACTIVATION_DISABLED: not authorized or not configured"}
        if not self.window_safe(self.clock()):
            return {"ok": False, "detail": "inside a protected opening window"}
        try:
            self._verify_candidate(artifact_path, checksum)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return {"ok": False, "detail": f"candidate refused ({type(exc).__name__})"}
        before = self.probe()
        if not pr._live_healthy(before):
            return {"ok": False, "detail": "live service not HEALTHY/READY/signals-only before switch"}
        previous = self._read_pointer()
        if previous is None:
            return {"ok": False, "detail": "no verified rollback reference (bootstrap missing)"}
        new = {"checksum": checksum, "file_sha": file_sha(artifact_path), "artifact": str(artifact_path)}
        if self._switch_to(new):
            return {"ok": True, "detail": f"switched to {checksum[:12]} (verified model file)"}
        restored = self._switch_to(previous)
        return {"ok": False, "detail": "post-switch verification failed; previous model "
                + ("restored and verified" if restored else "RESTORE NOT VERIFIED - manual recovery required")}

    def rollback(self, artifact_path, checksum):
        if not self.enabled:
            return {"ok": False, "detail": "rollback path not configured"}
        record = {"checksum": checksum, "file_sha": file_sha(artifact_path), "artifact": str(artifact_path)}
        ok = self._switch_to(record)
        return {"ok": ok, "detail": "rolled back and verified" if ok else "rollback NOT verified"}

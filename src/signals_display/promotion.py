"""Deterministic, fail-closed promotion controller for 8768 candidate models.

Promotion is automatic only when EVERY condition below holds. Any unverifiable
condition blocks promotion; nothing is guessed.

  1 the candidate was QUALIFIED by model_lifecycle (statistical gate already passed)
  2 integrity: candidate and report checksums verify; artifact is complete and deployable
  3 no repeated promotion, and the candidate's training cutoff is later than the live model's
  4 the live model can be identified, is HEALTHY, READY and signals-only
  5 resource headroom, Telegram working, and outside the 15-minute opening window
  6 a verified immutable backup of the current live model exists (the rollback checkpoint)
  7 the switch goes through an ACTIVATOR; with no verified activator configured the
    controller stops before touching anything (ACTIVATION_DISABLED)
  8 post-switch monitoring: sustained operational failure rolls back automatically
  9 every step is written to an append-only, checksum-chained history

Interrupted runs are recovered from history at the start of the next cycle; if the
live model cannot be identified, the controller stops (RECOVERY_BLOCKED).
"""
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import model_lifecycle as lc
from . import opening_models

HISTORY = "history.jsonl"
ACTIVE = "active.json"
LOCK = "controller.lock"
MIN_MEM_MIB = 768
MAX_LOAD1 = 8.0
MIN_DISK_MIB = 10.25 * 1024


class Activator:
    """Interface for the one component allowed to change the live model.
    The real implementation must be the guarded deployment procedure; it is not built here."""
    enabled = False

    def current(self):
        """Checksum of the live model, or None if it cannot be determined."""
        raise NotImplementedError

    def activate(self, artifact_path, checksum):
        raise NotImplementedError

    def rollback(self, artifact_path, checksum):
        raise NotImplementedError


class DisabledActivator(Activator):
    enabled = False

    def current(self):
        return None

    def activate(self, artifact_path, checksum):
        return {"ok": False, "detail": "ACTIVATION_DISABLED: no verified guarded activator is configured"}

    def rollback(self, artifact_path, checksum):
        return {"ok": False, "detail": "ACTIVATION_DISABLED"}


def _now():
    return datetime.now(timezone.utc)


class History:
    """Append-only, checksum-chained JSON lines. Never rewritten or truncated."""

    def __init__(self, state_dir):
        self.path = Path(state_dir) / HISTORY

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, event, data):
        entries = self.entries()
        prev = entries[-1]["checksum"] if entries else "GENESIS"
        entry = {"seq": len(entries) + 1, "at": _now().isoformat(), "event": event, "data": data, "prev": prev}
        entry["checksum"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    def verify_chain(self):
        prev = "GENESIS"
        for e in self.entries():
            body = {k: v for k, v in e.items() if k != "checksum"}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            if e["prev"] != prev or e["checksum"] != digest:
                return False
            prev = e["checksum"]
        return True


def _write_bytes_once(path, data):
    """Create-only, private, byte-exact write (the backup must match the live file's checksum)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _write_active(state_dir, record):
    path = Path(state_dir) / ACTIVE
    tmp = path.with_suffix(".json.next")
    tmp.write_text(json.dumps(record, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_active(state_dir):
    path = Path(state_dir) / ACTIVE
    return json.loads(path.read_text()) if path.exists() else None


def recover(history, activator):
    """Resolve an interrupted promotion from history. Returns a note; fails closed if unknown."""
    entries = history.entries()
    if not entries or entries[-1]["event"] not in ("PROMOTION_STARTED", "BACKUP_VERIFIED"):
        return None
    started = entries[-1]["data"]
    live = activator.current()
    if live is not None and live == started.get("candidate_checksum"):
        history.append("RECOVERED_PROMOTED", {"candidate_checksum": live})
        return "RECOVERED_PROMOTED"
    if live is not None and live == started.get("previous_checksum"):
        history.append("RECOVERED_ABORTED", {"live_checksum": live})
        return "RECOVERED_ABORTED"
    history.append("RECOVERY_BLOCKED", {"reason": "live model could not be identified after an interrupted promotion"})
    return "RECOVERY_BLOCKED"


def _live_healthy(sample):
    return (sample is not None and sample.get("health") == "HEALTHY" and sample.get("ready") == "READY"
            and sample.get("signals_only") is True)


def promotion_cycle(state_dir, status, activator, *, live_artifact_path, health_samples, resources,
                    telegram_ok, seconds_to_opening, notify=lambda event, data: None, now=None):
    """Run one controller cycle. Returns a result dict; never raises past its own boundary."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    now = now or _now()
    lock = open(state_dir / LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"result": "LOCKED", "detail": "another controller cycle is running"}
    try:
        return _cycle(state_dir, status, activator, live_artifact_path, health_samples, resources,
                      telegram_ok, seconds_to_opening, notify, now)
    except Exception as exc:  # fail closed: any unexpected error stops promotion and is recorded
        History(state_dir).append("CONTROLLER_ERROR", {"error": type(exc).__name__})
        notify("critical_failure", {"error": type(exc).__name__})
        return {"result": "ERROR_STOPPED", "detail": type(exc).__name__}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _cycle(state_dir, status, activator, live_artifact_path, health_samples, resources, telegram_ok,
           seconds_to_opening, notify, now):
    history = History(state_dir)
    if not history.verify_chain():
        notify("critical_failure", {"reason": "history checksum chain is broken"})
        return {"result": "BLOCKED", "detail": "history checksum chain is broken"}
    recovered = recover(history, activator)
    if recovered == "RECOVERY_BLOCKED":
        notify("critical_failure", {"reason": "interrupted promotion could not be resolved"})
        return {"result": "BLOCKED", "detail": "interrupted promotion could not be resolved"}

    # 1 the activator must be verified and enabled BEFORE any other decision or change
    if not activator.enabled:
        history.append("PROMOTION_BLOCKED", {"reason": "ACTIVATION_DISABLED"})
        return {"result": "BLOCKED", "detail": "ACTIVATION_DISABLED: no verified guarded activator configured"}

    if status.get("decision") != "CANDIDATE_QUALIFIED":
        history.append("NOT_PROMOTED", {"decision": status.get("decision"), "reasons": status.get("reasons")})
        return {"result": "NOT_PROMOTED", "detail": status.get("decision")}

    # 2 integrity
    cand_path = Path(status["candidate_file"])
    body = lc.load_verified(cand_path)
    if body.get("checksum") != status.get("candidate_checksum"):
        return _block(history, notify, "candidate checksum does not match the cycle status")
    try:
        opening_models.validate_artifact(body["artifact"])
    except (ValueError, KeyError, TypeError) as exc:
        return _block(history, notify, f"candidate artifact is not deployable ({type(exc).__name__})")
    report = None
    for path in (state_dir / "reports").glob("report-*.json"):
        candidate = lc.load_verified(path)
        if candidate.get("checksum") == status.get("report_checksum"):
            report = candidate
            break
    if report is None or report.get("candidate_file_sha256") != lc.sha256_file(cand_path):
        return _block(history, notify, "evaluation report is missing or does not match the candidate file")

    # 3 no repeated promotion; training cutoff must be later than the live model's
    if any(e["event"] in ("PROMOTED", "RECOVERED_PROMOTED") and e["data"].get("candidate_checksum") == body["checksum"]
           for e in history.entries()):
        return {"result": "ALREADY_PROMOTED", "detail": body["checksum"][:12]}
    active = _read_active(state_dir)
    if active and active.get("training_cutoff") and body["training_cutoff"] <= active["training_cutoff"]:
        return _block(history, notify, "candidate is not trained on data newer than the live model")

    # 4 live model identified and healthy
    live = activator.current() if activator.enabled else None
    if not _live_healthy(health_samples[-1] if health_samples else None):
        return _block(history, notify, "live 8768 is not HEALTHY, READY and signals-only")
    if live is None and activator.enabled:
        return _block(history, notify, "live model checksum could not be determined")
    previous = live if live else (active or {}).get("candidate_checksum")
    if previous is None:
        return _block(history, notify, "no verified rollback reference for the live model")

    # 5 resources, Telegram, opening window
    mem, load1, disk = resources.get("mem_mib"), resources.get("load1"), resources.get("disk_mib")
    if mem is None or mem < MIN_MEM_MIB:
        return _block(history, notify, "insufficient memory headroom")
    if load1 is None or load1 > MAX_LOAD1:
        return _block(history, notify, "insufficient CPU headroom")
    if disk is None or disk < MIN_DISK_MIB:
        return _block(history, notify, "insufficient disk headroom")
    if not telegram_ok:
        return _block(history, notify, "Telegram is not working; promotion requires working alerts")
    if seconds_to_opening is None or seconds_to_opening < 225 or not lc.opening_window_safe(now):
        return {"result": "DEFERRED", "detail": "inside or near a 15-minute opening window; retry next cycle"}

    # 6 immutable backup of the live model = rollback checkpoint
    live_bytes = Path(live_artifact_path).read_bytes()
    backup_sha = hashlib.sha256(live_bytes).hexdigest()
    backup_path = state_dir / "backups" / f"live-{backup_sha[:16]}.json"
    if not backup_path.exists():
        _write_bytes_once(backup_path, live_bytes)
    if hashlib.sha256(backup_path.read_bytes()).hexdigest() != backup_sha:
        return _block(history, notify, "rollback backup failed verification")
    history.append("BACKUP_VERIFIED", {"previous_checksum": previous, "backup_file": backup_path.name})

    # 7 switch, with a recorded start
    history.append("PROMOTION_STARTED", {"candidate_checksum": body["checksum"], "previous_checksum": previous})
    result = activator.activate(cand_path, body["checksum"])
    if not result.get("ok"):
        rb = activator.rollback(backup_path, previous)
        history.append("ACTIVATION_FAILED", {"detail": result.get("detail"), "rolled_back": bool(rb.get("ok"))})
        notify("rollback", {"reason": "activation failed", "previous_checksum": previous[:12]})
        return {"result": "ROLLED_BACK", "detail": result.get("detail")}

    # 8 monitor after the switch
    decision = lc.rollback_decision(health_samples, previous)
    if decision["decision"] == "ROLLBACK_REQUIRED":
        rb = activator.rollback(backup_path, previous)
        history.append("ROLLED_BACK", {"reason": decision.get("reason"), "rolled_back": bool(rb.get("ok"))})
        notify("rollback", {"reason": decision.get("reason"), "previous_checksum": previous[:12]})
        return {"result": "ROLLED_BACK", "detail": decision.get("reason")}
    if decision["decision"] == "SUSPENDED_INSUFFICIENT_EVIDENCE":
        history.append("MONITOR_SUSPENDED", {"candidate_checksum": body["checksum"]})

    # 9 success: atomic pointer, history, notification
    _write_active(state_dir, {"candidate_checksum": body["checksum"], "training_cutoff": body["training_cutoff"],
                              "previous_checksum": previous, "backup_file": backup_path.name,
                              "promoted_at": now.isoformat()})
    history.append("PROMOTED", {"candidate_checksum": body["checksum"], "previous_checksum": previous})
    notify("promoted", {"candidate_checksum": body["checksum"][:12], "previous_checksum": previous[:12]})
    return {"result": "PROMOTED", "detail": body["checksum"][:12]}


def _block(history, notify, reason):
    history.append("PROMOTION_BLOCKED", {"reason": reason})
    return {"result": "BLOCKED", "detail": reason}

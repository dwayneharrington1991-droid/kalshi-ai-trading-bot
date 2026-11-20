"""Post-promotion watch: keeps a promoted model under observation across cycles and rolls back on failure.

A promotion is not final when the switch succeeds. The watch continues every cycle until one of:

  ROLLBACK_REQUIRED  - any of these (fail closed):
       * the served model file sha256 differs from the promoted file (integrity), at once
       * health, readiness, signals-only, worker health or latency fails in CONSECUTIVE_OPERATIONAL
         consecutive samples
       * paired performance on post-promotion markets: the promoted model is significantly worse
         than the previous model (one-sided z <= -Z_DEGRADE) once WATCH_MIN_PAIRED markets exist
       * calibration (ECE) of the promoted model exceeds the previous model's by more than
         MAX_CALIBRATION_DEGRADATION once WATCH_MIN_PAIRED markets exist
  PASSED             - WATCH_MAX_MARKETS post-promotion markets with no failure
  WATCHING           - otherwise (including too little evidence: no performance decision is invented)

Only evidence measured AFTER the switch counts. Rows are used only when their label was available
after promoted_at, so nothing that was known before the switch is reused. One watch at a time: a new
candidate cannot be activated while a watch is open.
"""
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from . import model_lifecycle as lc
from . import opening_models

WATCH_FILE = "watch.json"
MAX_LATENCY_MS = 3000
MAX_QUOTE_AGE_S = 10
CONSECUTIVE_OPERATIONAL = 2
WATCH_MIN_PAIRED = 50
WATCH_MAX_MARKETS = 150
Z_DEGRADE = 2.576            # one-sided 0.5%: repeated looks need a stricter bar than a single test


def _write_atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".watch-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_watch(state_dir):
    path = Path(state_dir) / WATCH_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text())


def start_watch(state_dir, *, candidate_checksum, candidate_file, promoted_file_sha, previous_checksum,
                backup_file, promoted_at):
    record = {"state": "WATCHING", "candidate_checksum": candidate_checksum, "candidate_file": str(candidate_file),
              "promoted_file_sha": promoted_file_sha, "previous_checksum": previous_checksum,
              "backup_file": str(backup_file), "promoted_at": promoted_at, "consecutive_bad": 0, "cycles": 0}
    _write_atomic(Path(state_dir) / WATCH_FILE, record)
    return record


def finish_watch(state_dir, watch, state, reason):
    watch = {**watch, "state": state, "reason": reason}
    _write_atomic(Path(state_dir) / WATCH_FILE, watch)
    return watch


def operational_bad(sample, expected_sha):
    if sample is None:
        return True
    latency = sample.get("latency_ms")
    return (sample.get("health") != "HEALTHY" or sample.get("ready") != "READY"
            or sample.get("signals_only") is not True or sample.get("workers_healthy") is False
            or latency is None or latency > MAX_LATENCY_MS
            or (sample.get("quote_age_s") is not None and sample["quote_age_s"] > MAX_QUOTE_AGE_S)
            or sample.get("model_file_sha") != expected_sha)


def integrity_bad(sample, expected_sha):
    """Served model differs from the promoted file. Immediate rollback, no consecutive count."""
    return sample is not None and sample.get("model_file_sha") not in (None, expected_sha)


def _paired_stats(new_artifact, old_artifact, rows):
    """Paired log-loss gain (old - new; positive = promoted better) and calibration on the same rows."""
    diffs, new_pairs, old_pairs = [], [], []
    for r in rows:
        y = int(r["outcome"])
        p_new = float(opening_models.predict(new_artifact["model"], r["features"], baseline_probability=r["baseline"]))
        p_old = float(opening_models.predict(old_artifact["model"], r["features"], baseline_probability=r["baseline"]))
        diffs.append(lc._logloss_row(p_old, y) - lc._logloss_row(p_new, y))
        new_pairs.append((p_new, y))
        old_pairs.append((p_old, y))
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    if var > 0:
        z = mean / math.sqrt(var / n)
    else:                                  # identical differences: finite sentinel, never inf in the JSON record
        z = 0.0 if mean == 0 else math.copysign(1e6, mean)
    return {"paired_markets": n, "logloss_gain_mean": round(mean, 6), "logloss_gain_z": round(z, 4),
            "ece_promoted": round(lc._ece(new_pairs), 5), "ece_previous": round(lc._ece(old_pairs), 5)}


def evaluate(watch, samples, *, promoted_artifact, previous_artifact, rows):
    """Pure decision for one cycle. Returns (decision, reason, evidence, updated_watch)."""
    expected = watch["promoted_file_sha"]
    evidence = {"samples": len(samples)}
    for s in samples:
        if integrity_bad(s, expected):
            return "ROLLBACK_REQUIRED", "served model file does not match the promoted model", evidence, watch
    consecutive = watch.get("consecutive_bad", 0)
    for s in samples:
        consecutive = consecutive + 1 if operational_bad(s, expected) else 0
        if consecutive >= CONSECUTIVE_OPERATIONAL:
            return ("ROLLBACK_REQUIRED", f"{CONSECUTIVE_OPERATIONAL} consecutive operational failures "
                    "(health, readiness, signals-only, workers, latency or quote age)", evidence,
                    {**watch, "consecutive_bad": consecutive})
    updated = {**watch, "consecutive_bad": consecutive, "cycles": watch.get("cycles", 0) + 1}
    promoted_at = datetime.fromisoformat(watch["promoted_at"])
    post = [r for r in (rows or []) if r.get("label_available_at") is not None
            and datetime.fromisoformat(str(r["label_available_at"])) > promoted_at]
    evidence["post_promotion_markets"] = len(post)
    if promoted_artifact is not None and previous_artifact is not None and len(post) >= WATCH_MIN_PAIRED:
        stats = _paired_stats(promoted_artifact, previous_artifact, post)
        evidence.update(stats)
        if stats["logloss_gain_z"] <= -Z_DEGRADE:
            return "ROLLBACK_REQUIRED", "promoted model is significantly worse than the previous model", evidence, updated
        if stats["ece_promoted"] - stats["ece_previous"] > lc.MAX_CALIBRATION_DEGRADATION:
            return "ROLLBACK_REQUIRED", "promoted model calibration is worse than the previous model", evidence, updated
    else:
        evidence["performance"] = "SUSPENDED: fewer than %d post-promotion markets or artifacts unavailable" % WATCH_MIN_PAIRED
    if len(post) >= WATCH_MAX_MARKETS:
        return "PASSED", "no failure across the watch window", evidence, updated
    return "WATCHING", "watch continues", evidence, updated

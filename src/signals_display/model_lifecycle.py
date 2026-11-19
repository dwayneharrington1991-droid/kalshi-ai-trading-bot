"""Automatic retraining, shadow evaluation and fail-closed candidate qualification for 8768.

This module never changes the live model, deploys, restarts a service, or places
orders. Promotion is a separate controller (promotion.py) that only acts on a
candidate this module has QUALIFIED, and only through an activator interface.

  retrain   reuse the existing opening_models.evaluate (chronological 60/20/20
            split, label-availability purging and its own gates). If it yields no
            candidate, the current model is kept and that is recorded.
  store     a candidate is written ONCE as an immutable, checksummed file.
  shadow    candidate and recorded production calls are scored on the SAME markets,
            only where the prediction was generated after the candidate's training
            cutoff and the outcome was observed after that prediction.
  qualify   fail closed. Requires leakage-free data, a complete deployable artifact,
            the existing 55% threshold, at least 100 unseen markets, lower log loss
            and Brier than production, no calibration degradation, a paired
            per-market log-loss gain whose z-score clears a Bonferroni correction over
            every candidate evaluated (to block lucky winning streaks), and a gain
            that holds in BOTH chronological halves of the shadow window.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from statistics import NormalDist
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from . import learning_loop as ll
from . import opening_models
from . import opening_research as research
from .scores import wilson_interval

FORMAT = "kxbtc-lifecycle-8768-v1"
MIN_FRESH_MARKETS = 100
LIVE_THRESHOLD = 0.55            # the production signal threshold; changing it needs separate authorization
MAX_CALIBRATION_DEGRADATION = 0.05
MIN_NEW_SETTLED = 50             # new settled markets required before a retraining attempt
ALPHA = 0.05
MIN_GAIN = 0.0                   # mean per-market log-loss gain must be strictly positive
EPS = 1e-6


def canonical_checksum(obj):
    body = {k: v for k, v in obj.items() if k != "checksum"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_once(path, obj):
    """Create-only, private, durable write. Never replaces an existing file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=1, sort_keys=True, default=str))
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return sha256_file(path)


def load_verified(path):
    """Load a stored record and verify its own checksum; fail closed on any mismatch."""
    obj = json.loads(Path(path).read_text())
    if not isinstance(obj, dict) or obj.get("checksum") != canonical_checksum(obj):
        raise ValueError(f"{path} failed its checksum: refusing to use it")
    return obj


def point_in_time_rows(rows, cutoff):
    """Keep only rows generated after the cutoff whose outcome was observed after the prediction."""
    kept = []
    excluded = {"NOT_AFTER_CUTOFF": 0, "OUTCOME_NOT_AFTER_PREDICTION": 0, "NO_OUTCOME": 0}
    for r in rows:
        if r.get("outcome") not in (0, 1):
            excluded["NO_OUTCOME"] += 1
            continue
        generated = datetime.fromisoformat(str(r["timestamp"]))
        observed = datetime.fromisoformat(str(r["label_available_at"]))
        if generated <= cutoff:
            excluded["NOT_AFTER_CUTOFF"] += 1
        elif observed <= generated:
            excluded["OUTCOME_NOT_AFTER_PREDICTION"] += 1
        else:
            kept.append(r)
    return kept, excluded


def leakage_violations(rows, cutoff):
    bad = 0
    for r in rows:
        generated = datetime.fromisoformat(str(r["timestamp"]))
        observed = datetime.fromisoformat(str(r["label_available_at"]))
        if generated <= cutoff or observed <= generated:
            bad += 1
    return bad


def _logloss_row(p, y):
    p = min(max(float(p), EPS), 1 - EPS)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _ece(pairs, bins=5):
    if not pairs:
        return None
    total, err = len(pairs), 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if members:
            err += len(members) / total * abs(sum(p for p, _ in members) / len(members)
                                              - sum(y for _, y in members) / len(members))
    return err


def _summary(pairs):
    if not pairs:
        return {"log_loss": None, "brier": None, "ece": None}
    ll_ = sum(_logloss_row(p, y) for p, y in pairs) / len(pairs)
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    return {"log_loss": ll_, "brier": brier, "ece": _ece(pairs)}


def _calls(pairs, threshold):
    calls = [(p, y) for p, y in pairs if max(p, 1 - p) >= threshold]
    correct = sum((p >= 0.5) == bool(y) for p, y in calls)
    return correct, len(calls)


def _paired_z(diffs):
    n = len(diffs)
    if n < 2:
        return None
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        return None if mean == 0 else math.copysign(math.inf, mean)
    return mean / math.sqrt(var / n)


def shadow_metrics(artifact, rows):
    """Candidate and recorded production calls scored on the SAME point-in-time rows."""
    cand, prod, paired, paired_pos = [], [], [], []
    for pos, r in enumerate(rows):
        p = float(opening_models.predict(artifact["model"], r["features"], baseline_probability=r["baseline"]))
        y = int(r["outcome"])
        cand.append((p, y))
        prodable = r.get("recorded_up") is not None and r.get("recorded_direction") in ("UP", "DOWN") \
            and r.get("recorded_confidence_status") in ll.DISPLAYABLE
        if prodable:
            up = float(r["recorded_up"])
            prod.append((up, y))
            paired.append(_logloss_row(up, y) - _logloss_row(p, y))   # positive = candidate better
            paired_pos.append(pos)
    # Stability: the same gain must hold in BOTH chronological halves of the window.
    half = len(rows) // 2
    first = [d for d, pos in zip(paired, paired_pos) if pos < half]
    second = [d for d, pos in zip(paired, paired_pos) if pos >= half]
    stable = (len(first) >= 20 and len(second) >= 20
              and sum(first) / len(first) > 0 and sum(second) / len(second) > 0)
    threshold = artifact["decision_policy"]["threshold"]
    cand_correct, cand_calls = _calls(cand, threshold)
    prod_correct, prod_calls = _calls(prod, LIVE_THRESHOLD)
    c, pr = _summary(cand), _summary(prod)
    return {
        "markets": len(rows),
        "paired_markets": len(paired),
        "paired_logloss_gain_mean": (sum(paired) / len(paired)) if paired else None,
        "paired_logloss_gain_z": _paired_z(paired),
        "stable_two_windows": stable,
        "candidate": {**c, "calls": cand_calls, "correct": cand_correct,
                      "coverage": cand_calls / len(rows) if rows else 0,
                      "accuracy": cand_correct / cand_calls if cand_calls else None,
                      "accuracy_wilson_95": wilson_interval(cand_correct, cand_calls) if cand_calls else None,
                      "threshold": threshold},
        "production": {**pr, "calls": prod_calls, "correct": prod_correct,
                       "accuracy": prod_correct / prod_calls if prod_calls else None,
                       "accuracy_wilson_95": wilson_interval(prod_correct, prod_calls) if prod_calls else None,
                       "threshold": LIVE_THRESHOLD, "scored_rows": len(prod)},
    }


def bonferroni_critical_z(candidates_evaluated):
    """One-sided critical z after correcting alpha for every candidate ever evaluated."""
    k = max(1, int(candidates_evaluated))
    return NormalDist().inv_cdf(1 - ALPHA / k)


def gate(artifact, checksum, metrics, violations, candidates_evaluated):
    """Fail-closed qualification. Returns (decision, reasons). Never promotes by itself."""
    policy = artifact.get("decision_policy") if isinstance(artifact, dict) else None
    threshold = policy.get("threshold") if isinstance(policy, dict) else None
    if threshold is not None and abs(threshold - LIVE_THRESHOLD) > 1e-12:
        return "REJECTED_THRESHOLD_CHANGE", [
            f"candidate threshold {threshold} differs from the live {LIVE_THRESHOLD}; changing it needs separate authorization"]
    try:
        opening_models.validate_artifact(artifact)
    except (ValueError, KeyError, TypeError) as exc:
        return "REJECTED_NOT_DEPLOYABLE", [f"incomplete or non-deployable artifact ({type(exc).__name__})"]
    if not checksum:
        return "REJECTED_INCOMPLETE", ["candidate has no checksum"]
    if violations:
        return "REJECTED_LEAKAGE", [f"{violations} point-in-time violation(s)"]
    if metrics["markets"] < MIN_FRESH_MARKETS:
        return "INSUFFICIENT_EVIDENCE", [f"{metrics['markets']} unseen markets (need {MIN_FRESH_MARKETS})"]
    cand, prod = metrics["candidate"], metrics["production"]
    if cand["log_loss"] is None or prod["log_loss"] is None or metrics["paired_markets"] < MIN_FRESH_MARKETS:
        return "INSUFFICIENT_EVIDENCE", ["too few markets with recorded production calls to compare"]
    reasons = []
    if cand["log_loss"] >= prod["log_loss"]:
        reasons.append("log loss is not lower than production on the same markets")
    if cand["brier"] > prod["brier"]:
        reasons.append("Brier score is worse than production")
    if cand["ece"] is not None and prod["ece"] is not None and cand["ece"] > prod["ece"] + MAX_CALIBRATION_DEGRADATION:
        reasons.append("calibration is degraded beyond the allowed margin")
    if prod["accuracy"] is not None and cand["accuracy_wilson_95"] is not None \
            and cand["accuracy_wilson_95"][0] <= prod["accuracy"]:
        reasons.append("candidate accuracy lower bound does not exceed production accuracy")
    z = metrics["paired_logloss_gain_z"]
    crit = bonferroni_critical_z(candidates_evaluated)
    if z is None or z <= crit:
        reasons.append(f"paired gain z={z} does not clear the Bonferroni critical value {crit:.3f} "
                       f"for {candidates_evaluated} candidate(s) evaluated")
    if not metrics["stable_two_windows"]:
        reasons.append("the gain does not hold in both chronological halves of the shadow window")
    if reasons:
        return "REJECTED_PERFORMANCE", reasons
    return "CANDIDATE_QUALIFIED", ["meets every statistical and integrity rule on unseen markets"]


def state_snapshot(journal, asof):
    """Read-only, point-in-time settled-market rows from the journal."""
    with closing(sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        return research.load(db, asof)


def retrain_cycle(journal, state_dir, asof, commit, last_cutoff=None, min_new=MIN_NEW_SETTLED, candidates_evaluated=0):
    """One scheduled cycle. Writes only new, immutable files under state_dir."""
    state_dir = Path(state_dir)
    rows = state_snapshot(journal, asof)
    new = [r for r in rows if last_cutoff is None
           or datetime.fromisoformat(str(r["label_available_at"])) > last_cutoff]
    status = {"format": FORMAT, "generated_at": asof.isoformat(), "commit": commit,
              "settled_markets": len(rows), "new_settled_markets": len(new),
              "live_model": "UNCHANGED", "candidates_evaluated_before": candidates_evaluated}
    if len(new) < min_new:
        status["decision"] = "SKIPPED_NOT_ENOUGH_NEW_DATA"
        return status
    candidate, report = opening_models.evaluate(rows)
    status["opening_model_status"] = report.get("status")
    if candidate is None:
        status["decision"] = "NO_CANDIDATE"
        status["reasons"] = ["the existing chronological training path produced no candidate; the current model is kept"]
        return status
    body = {"format": FORMAT, "kind": "CANDIDATE", "training_cutoff": asof.isoformat(), "commit": commit,
            "artifact": candidate}
    body["checksum"] = canonical_checksum(body)
    cand_sha = write_once(state_dir / "candidates" / f"candidate-{body['checksum'][:16]}.json", body)
    shadow_rows, excluded = point_in_time_rows(rows, asof)
    violations = leakage_violations(shadow_rows, asof)
    metrics = shadow_metrics(candidate, shadow_rows)
    decision, reasons = gate(candidate, body["checksum"], metrics, violations, candidates_evaluated + 1)
    report_obj = {"format": FORMAT, "kind": "EVALUATION", "candidate_checksum": body["checksum"],
                  "candidate_file_sha256": cand_sha, "training_cutoff": asof.isoformat(),
                  "excluded": excluded, "leakage_violations": violations, "metrics": metrics,
                  "decision": decision, "reasons": reasons, "commit": commit,
                  "candidates_evaluated": candidates_evaluated + 1}
    report_obj["checksum"] = canonical_checksum(report_obj)
    write_once(state_dir / "reports" / f"report-{report_obj['checksum'][:16]}.json", report_obj)
    status.update({"decision": decision, "reasons": reasons, "candidate_checksum": body["checksum"],
                   "report_checksum": report_obj["checksum"], "markets_scored": metrics["markets"],
                   "candidate_file": str(state_dir / "candidates" / f"candidate-{body['checksum'][:16]}.json")})
    return status


def rollback_decision(samples, previous_checksum, consecutive=3, max_quote_age=10):
    """Post-promotion monitor. Rolls back on sustained operational failure; suspends on too little evidence."""
    if not samples:
        return {"decision": "SUSPENDED_INSUFFICIENT_EVIDENCE", "rollback_to": previous_checksum}
    run = 0
    for s in samples:
        bad = (s.get("health") != "HEALTHY" or s.get("ready") != "READY" or not s.get("signals_only")
               or (s.get("quote_age_s") is not None and s["quote_age_s"] > max_quote_age)
               or s.get("workers_healthy") is False)
        run = run + 1 if bad else 0
        if run >= consecutive:
            return {"decision": "ROLLBACK_REQUIRED", "rollback_to": previous_checksum,
                    "reason": f"{consecutive} consecutive operational failures"}
    return {"decision": "OK", "rollback_to": previous_checksum}


SCHEDULE_MINUTES = (7, 22, 37, 52)   # every 15 minutes, all outside the guarded boundary windows


def opening_window_safe(now):
    """No model change within 3 minutes after a 15-minute boundary or in the final minute before one."""
    since = (now.minute % 15) * 60 + now.second
    return since >= 180 and (900 - since) >= 60


def publish(path, status):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".lifecycle-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(status, f, indent=1, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--asof")
    parser.add_argument("--min-new", type=int, default=MIN_NEW_SETTLED)
    args = parser.parse_args(argv)
    asof = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    status = retrain_cycle(args.journal, args.state_dir, asof, args.commit, min_new=args.min_new)
    publish(Path(f"{args.journal}.lifecycle-status.json"), status)
    print(json.dumps({k: status.get(k) for k in ("decision", "reasons", "new_settled_markets")}, default=str))


if __name__ == "__main__":
    main()

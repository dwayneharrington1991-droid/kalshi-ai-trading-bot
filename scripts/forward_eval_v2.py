"""Pre-registered forward test v2 (research only). Frozen 2026-11-20 by commit; see docs/preregistration-v2-20261120.md.

Candidates (defined in scripts/research_opening_v2.py, refit walk-forward every 12 openings on all
openings settled before each one): "ensemble" (mean of core16 ridge, all-87 ridge and shallow
gradient boosting), "gbm", "stable_ridge". Compared with the live model's immutable P(UP).
Only openings with opens_at >= FREEZE_V2 count. Independent of, and never changes, test v1 (c08fb8c).

  python scripts/forward_eval_v2.py --old old_slim.json --new journal_dump.json --artifact live_artifact.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_opening_v2 as rv  # noqa: E402

FREEZE_V2 = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
CANDIDATES = ("ensemble", "gbm", "stable_ridge", "blend")   # blend = 0.5 x live P(UP) + 0.5 x ensemble
MIN_FORWARD = 200


def run(old, new, artifact):
    rows = rv.load([old, new], freeze=datetime(2100, 1, 1, tzinfo=timezone.utc))
    D = rv.Design(rows, json.load(open(artifact)))
    idx, P, _timing, _keep = rv.walk_forward(rows, D, eval_start=FREEZE_V2)
    report = {"freeze": FREEZE_V2.isoformat(), "forward_openings": len(idx), "minimum_for_decision": MIN_FORWARD}
    if not idx:
        report["decision"] = "NOT ENOUGH FORWARD DATA"
        return report
    live_all = np.array([r["live"] if r["live"] is not None else np.nan for r in rows])
    P = dict(P)
    P["blend"] = 0.5 * live_all + 0.5 * P["ensemble"]
    ev = rv.evaluate(rows, idx, {k: P[k] for k in CANDIDATES})
    report["evaluation"] = ev
    # Secondary (descriptive, pre-registered): live UP/DOWN calls that the ensemble agrees with.
    y = np.array([rows[i]["y"] for i in idx])
    live = np.array([rows[i]["live"] if rows[i]["live"] is not None else np.nan for i in idx])
    ens = P["ensemble"][idx]
    calls = (live >= 0.55) | (live <= 0.45)
    agree = calls & (np.sign(ens - 0.5) == np.sign(live - 0.5))
    report["secondary_confirmed_live_calls"] = {
        "all_live_calls": int(calls.sum()),
        "all_live_accuracy": float(((live >= 0.5) == (y == 1))[calls].mean()) if calls.any() else None,
        "confirmed_calls": int(agree.sum()),
        "confirmed_accuracy": float(((live >= 0.5) == (y == 1))[agree].mean()) if agree.any() else None,
        "note": "descriptive only; selected from pre-freeze data (68.7% on 83 confirmed vs 65.1% on 106), so only forward data can confirm it",
    }
    passed = [k for k in CANDIDATES
              if len(idx) >= MIN_FORWARD and ev["models"][k]["brier_minus_live_ci95"][1] < 0]
    report["passed_preregistered_rule"] = passed
    report["decision"] = ("ELIGIBLE FOR A SEPARATE PROMOTION REVIEW" if passed else
                          "NOT ENOUGH FORWARD DATA" if len(idx) < MIN_FORWARD else "NO CANDIDATE PASSED")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--artifact", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(run(a.old, a.new, a.artifact), indent=1, default=str))


if __name__ == "__main__":
    main()

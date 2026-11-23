"""Research only: AI #1 fix candidates (masked, no_logistic, aligned) on NON-reserved windows (2026-11-23).

Uses the frozen v3/v4 candidate code. Skew window = after the 2026-11-20 20:21 input change and before the
v3 freeze (2026-11-22 01:00), so no forward-test outcomes are touched. Pre-change window = reset-window inputs.
"""
import json, sys
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, "scripts"); sys.path.insert(0, ".")
import forward_eval_v3 as v3
import forward_eval_v4 as v4
D = sys.argv[1] if len(sys.argv) > 1 else "../forward-v2/20261123T020628Z/"
art = json.load(open(D + "live_artifact.json")); align = json.load(open(str(v4.ALIGNMENT)))
rows = v3.load(D + "journal_dump.json", freeze=datetime(2026, 11, 16, tzinfo=timezone.utc))
CHANGE = datetime(2026, 11, 20, 20, 21, tzinfo=timezone.utc)
def evaluate(R, label):
    y = np.array([r["y"] for r in R]); P = {"live": np.array([r["live"] for r in R])}
    cand = [v3.candidate_probabilities(art, r["features"], r["live"]) for r in R]
    P["masked"] = np.array([c["masked"] for c in cand]); P["no_logistic"] = np.array([c["no_logistic"] for c in cand])
    if label.startswith("skew"):
        P["aligned"] = np.array([v4.aligned_probability(art, align, r["features"], r["live"]) for r in R])
    print(f"{label}: n {len(R)}, UP rate {y.mean():.3f}, base Brier {y.mean()*(1-y.mean()):.4f}")
    rng = np.random.default_rng(11)
    for k, p in P.items():
        c = v4._calls(p, y); b = float(np.mean((p - y) ** 2))
        d = (p - y) ** 2 - (P["live"] - y) ** 2
        bs = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(3000)]
        ci = "" if k == "live" else f" dBrier {d.mean():+.4f} [{np.percentile(bs,2.5):+.4f},{np.percentile(bs,97.5):+.4f}]"
        print(f"  {k:11s} calls {c['calls']:3d} right {c['correct']:3d} wrong {c['calls']-c['correct']:3d} acc {c['accuracy'] or 0:.3f} "
              f"WAIT {len(R)-c['calls']:3d} UP-share {c['up_calls']/max(c['calls'],1):.2f} meanP {p.mean():.3f} Brier {b:.4f}{ci}")
evaluate([r for r in rows if r["opens"] < CHANGE], "pre-change (reset-window inputs, matches training)")
evaluate([r for r in rows if CHANGE <= r["opens"] < v3.FREEZE_V3], "skew window (full-window inputs, not reserved)")

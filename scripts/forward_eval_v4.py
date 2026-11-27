"""Pre-registered forward test v4: live model with the L13 inputs quantile-aligned (research only).

Candidate "aligned": the live artifact (no refit) applied to the frozen opening inputs after mapping the
30 skewed micro/flow inputs through research_artifacts/feature_alignment_20261122.json, which was built
from INPUT VALUES ONLY (training-period openings vs openings 2026-11-20 20:30 .. 2026-11-22 03:00).
The volatility-baseline member is recovered from the stored live P(UP); no outcome or later information
is used. See docs/preregistration-v4-20261122.md.

Only openings with opens_at >= FREEZE_V4 count. Decision at >= MIN_FORWARD openings: the candidate passes
only if the 95% bootstrap CI of Brier(aligned) - Brier(live) lies entirely below 0. Tests v1, v2 and v3
are independent and unchanged.

  python scripts/forward_eval_v4.py --journal journal_dump.json --artifact live_artifact.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.signals_display import feature_alignment  # noqa: E402
from src.signals_display.opening_models import predict  # noqa: E402

FREEZE_V4 = datetime(2026, 11, 22, 3, 30, tzinfo=timezone.utc)
MIN_FORWARD = 200
THRESHOLD = 0.55
ALIGNMENT = ROOT / "research_artifacts" / "feature_alignment_20261122.json"


def aligned_probability(artifact, alignment, features, live_up):
    model = artifact["model"]
    comps, w = model["components"], np.array(model["mixture_weights"], float)
    members = [predict(c, features, baseline_probability=0.5) for c in comps[:3]]
    baseline = (live_up - float(np.dot(w[:3], members))) / w[3]
    shifted = feature_alignment.apply(features, alignment)
    aligned = [predict(c, shifted, baseline_probability=0.5) for c in comps[:3]]
    return float(min(1.0, max(0.0, np.dot(w[:3], aligned) + w[3] * baseline)))


def load(journal, freeze=FREEZE_V4):
    d = json.load(open(journal))
    out = {r[0]: r[3] for r in d["forecast_outcomes"]["rows"]}
    rows, seen = [], set()
    for _m, ticker, _g, _c, _cl, up, _s, payload in d["btc_engine_opening"]["rows"]:
        p = json.loads(payload)
        values = (p.get("frozen_features") or {}).get("values") or {}
        opens = datetime.fromisoformat(str(p["opens_at"]).replace("Z", "+00:00"))
        if ticker in seen or not values or up is None or out.get(ticker) is None or opens < freeze:
            continue
        if not str(p.get("model", "")).startswith("opening-candidate"):
            continue
        seen.add(ticker)
        rows.append({"ticker": ticker, "opens": opens, "y": int(out[ticker]), "live": float(up), "features": values})
    rows.sort(key=lambda r: r["opens"])
    return rows


def _calls(p, y):
    up, down = p >= THRESHOLD, p <= 1 - THRESHOLD
    correct = int(((up & (y == 1)) | (down & (y == 0))).sum())
    n = int((up | down).sum())
    return {"calls": n, "correct": correct, "accuracy": correct / n if n else None,
            "up_calls": int(up.sum()), "up_correct": int((up & (y == 1)).sum()),
            "down_calls": int(down.sum()), "down_correct": int((down & (y == 0)).sum())}


def run(journal, artifact_path, alignment_path=ALIGNMENT):
    artifact = json.load(open(artifact_path))
    alignment = json.load(open(alignment_path))
    rows = load(journal)
    report = {"freeze": FREEZE_V4.isoformat(), "forward_openings": len(rows), "minimum_for_decision": MIN_FORWARD}
    if not rows:
        report["decision"] = "NOT ENOUGH FORWARD DATA"
        return report
    y = np.array([r["y"] for r in rows])
    live = np.array([r["live"] for r in rows])
    p = np.array([aligned_probability(artifact, alignment, r["features"], r["live"]) for r in rows])
    eps = 1e-6
    ll = lambda q: float(-np.mean(y * np.log(np.clip(q, eps, 1)) + (1 - y) * np.log(np.clip(1 - q, eps, 1))))
    diff = (p - y) ** 2 - (live - y) ** 2
    rng = np.random.default_rng(5)
    boots = [diff[rng.integers(0, len(diff), len(diff))].mean() for _ in range(3000)]
    ci = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
    passes = len(rows) >= MIN_FORWARD and ci[1] < 0
    report["live"] = {"brier": float(((live - y) ** 2).mean()), "log_loss": ll(live), "mean_p_up": float(live.mean()),
                      **_calls(live, y)}
    report["aligned"] = {"brier": float(((p - y) ** 2).mean()), "log_loss": ll(p), "mean_p_up": float(p.mean()),
                         "brier_minus_live": float(diff.mean()), "brier_minus_live_ci95": ci, **_calls(p, y),
                         "passes": passes}
    report["up_rate"] = float(y.mean())
    report["decision"] = ("NOT ENOUGH FORWARD DATA" if len(rows) < MIN_FORWARD else
                          "CANDIDATE PASSES (needs your approval before any change)" if passes else "CANDIDATE DOES NOT PASS")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", required=True)
    ap.add_argument("--artifact", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(run(args.journal, args.artifact), indent=2))


if __name__ == "__main__":
    main()

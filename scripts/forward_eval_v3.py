"""Pre-registered forward test v3: live model with the post-9a9a6ea input skew removed (research only).

Background (docs/diagnosis-20261122-input-skew.md): since deployment 9a9a6ea (2026-11-20 20:21 UTC)
the Coinbase/Kraken stream fixes make 30 micro-structure and trade-flow inputs always present and
computed over full windows. The live model was trained when these inputs were missing ~40-60% of the
time and computed over reset windows, so they now arrive 2-6 standard deviations from its training
statistics, and its logistic component's mean P(UP) jumped from 0.50 to 0.61.

Candidates, all computed from the immutable frozen opening inputs and the live artifact (no refit):
  "masked"        live ensemble with the 30 skewed inputs treated as missing (as often in training);
  "no_logistic"   live ensemble without its logistic component, other weights renormalised.
The live component predictions are reproduced exactly; the volatility-baseline member is recovered
from the stored live P(UP), so no outcome or later information is used.

Only openings with opens_at >= FREEZE_V3 count. Decision at >= MIN_FORWARD openings: a candidate
passes only if the 95% bootstrap CI of its Brier minus the live Brier lies entirely below 0.
Independent of tests v1 (c08fb8c) and v2 (2d4d007), which are not changed.

  python scripts/forward_eval_v3.py --journal journal_dump.json --artifact live_artifact.json
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.signals_display.opening_models import predict  # noqa: E402

FREEZE_V3 = datetime(2026, 11, 22, 1, 0, tzinfo=timezone.utc)
MIN_FORWARD = 200
THRESHOLD = 0.55
CANDIDATES = ("masked", "no_logistic")


def skewed_inputs(artifact):
    names = artifact["model"]["components"][0]["preprocessor"]["names"]
    return [n for n in names
            if n.startswith("micro_") or n in ("advanced_trade_intensity", "advanced_effective_spread")]


def candidate_probabilities(artifact, features, live_up):
    model = artifact["model"]
    comps, w = model["components"], np.array(model["mixture_weights"], float)
    members = [predict(c, features, baseline_probability=0.5) for c in comps[:3]]
    baseline = (live_up - float(np.dot(w[:3], members))) / w[3]      # recover the volatility baseline
    skew = set(skewed_inputs(artifact))
    kept = {k: v for k, v in features.items() if k not in skew}
    masked_members = [predict(c, kept, baseline_probability=0.5) for c in comps[:3]]
    w2 = w.copy()
    w2[0] = 0.0
    w2 /= w2.sum()
    clip = lambda p: float(min(1.0, max(0.0, p)))
    return {"masked": clip(np.dot(w[:3], masked_members) + w[3] * baseline),
            "no_logistic": clip(np.dot(w2[:3], members) + w2[3] * baseline)}


def load(journal, freeze=FREEZE_V3):
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


def _boot(d, n=3000, seed=5):
    rng = np.random.default_rng(seed)
    means = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(n)]
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


def _calls(p, y):
    up, down = p >= THRESHOLD, p <= 1 - THRESHOLD
    correct = int(((up & (y == 1)) | (down & (y == 0))).sum())
    n = int((up | down).sum())
    return {"calls": n, "correct": correct, "accuracy": correct / n if n else None,
            "up_calls": int(up.sum()), "up_correct": int((up & (y == 1)).sum()),
            "down_calls": int(down.sum()), "down_correct": int((down & (y == 0)).sum())}


def run(journal, artifact_path):
    artifact = json.load(open(artifact_path))
    rows = load(journal)
    report = {"freeze": FREEZE_V3.isoformat(), "forward_openings": len(rows), "minimum_for_decision": MIN_FORWARD}
    if not rows:
        report["decision"] = "NOT ENOUGH FORWARD DATA"
        return report
    y = np.array([r["y"] for r in rows])
    live = np.array([r["live"] for r in rows])
    cand = [candidate_probabilities(artifact, r["features"], r["live"]) for r in rows]
    eps = 1e-6
    ll = lambda p: float(-np.mean(y * np.log(np.clip(p, eps, 1)) + (1 - y) * np.log(np.clip(1 - p, eps, 1))))
    report["live"] = {"brier": float(((live - y) ** 2).mean()), "log_loss": ll(live), **_calls(live, y)}
    report["candidates"] = {}
    for name in CANDIDATES:
        p = np.array([c[name] for c in cand])
        diff = (p - y) ** 2 - (live - y) ** 2
        ci = _boot(diff)
        report["candidates"][name] = {"brier": float(((p - y) ** 2).mean()), "log_loss": ll(p),
                                      "brier_minus_live": float(diff.mean()), "brier_minus_live_ci95": ci,
                                      **_calls(p, y),
                                      "passes": len(rows) >= MIN_FORWARD and ci[1] < 0}
    report["decision"] = ("NOT ENOUGH FORWARD DATA" if len(rows) < MIN_FORWARD else
                          "CANDIDATE PASSES (needs your approval before any change)"
                          if any(c["passes"] for c in report["candidates"].values()) else "NO CANDIDATE PASSES")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", required=True)
    ap.add_argument("--artifact", required=True)
    args = ap.parse_args(argv)
    print(json.dumps(run(args.journal, args.artifact), indent=2))


if __name__ == "__main__":
    main()

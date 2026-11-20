"""Pre-registered forward evaluation of opening-model challengers (research only).

Reads a read-only JSON export of the 8768 journal (tables btc_engine_opening, forecast_outcomes,
opening_quote_snapshots, as written by the research export) plus the live opening artifact, and scores
frozen challenger definitions against the live model. Nothing here can change a live model, threshold,
journal or alert; it writes only the report it prints.

Protocol (frozen 2026-11-20, see docs/preregistration-20261120.md):
  * every prediction for opening i is fitted only on openings that opened at least 30 minutes earlier
    (both settled before opening i), so no outcome that was unknown at the opening is used
  * only openings with opens_at >= FREEZE count toward the decision; earlier ones are training only
  * candidates are fixed below and may not be edited after the freeze; a changed candidate is a new
    experiment with a new freeze time
"""
import argparse
import json
import math
import random
from datetime import datetime, timedelta

import numpy as np

FREEZE = "2026-11-20T06:15:00+00:00"
MIN_FORWARD = 200
FEE_RATE = 0.07
CANDIDATES = {
    "dist": {"inputs": "distance_scaled", "ridge": 1.0},
    "core16": {"inputs": "live price_core preprocessor (16)", "ridge": 20.0},
}


def _ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load_rows(export):
    outcomes = {r[0]: r[3] for r in export["forecast_outcomes"]["rows"]}
    quotes = {}
    for r in export["opening_quote_snapshots"]["rows"]:
        if r[6] is None or r[8] is None:
            continue
        if r[1] not in quotes or abs(r[4]) < abs(quotes[r[1]]["sec"]):
            quotes[r[1]] = {"sec": r[4], "up_ask": r[6], "down_ask": r[8]}
    rows = []
    for _mode, ticker, _gen, _cut, _close, up, _status, payload in export["btc_engine_opening"]["rows"]:
        p = json.loads(payload)
        values = (p.get("frozen_features") or {}).get("values") or {}
        if not values or up is None or outcomes.get(ticker) is None:
            continue
        rows.append({"ticker": ticker, "opens": _ts(p["opens_at"]), "live": float(up), "features": values,
                     "outcome": int(outcomes[ticker]), "quote": quotes.get(ticker)})
    rows.sort(key=lambda r: r["opens"])
    return rows


def ridge_logistic(X, y, ridge, iters=50):
    """Newton (IRLS) for L2-penalized logistic regression; the intercept is not penalized."""
    X1 = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(X1.shape[1])
    pen = np.full(X1.shape[1], ridge)
    pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X1 @ w, -30, 30)))
        grad = X1.T @ (p - y) + pen * w
        hess = (X1 * (p * (1 - p))[:, None]).T @ X1 + np.diag(pen) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def design(rows, name, artifact):
    if name == "dist":
        v = [r["features"].get("distance_scaled") for r in rows]
        return np.array([[float(x) if isinstance(x, (int, float)) and math.isfinite(x) else 0.0] for x in v])
    from src.signals_display.learning import transform
    prep = artifact["model"]["components"][1]["preprocessor"]
    k = len(prep["names"])
    return np.array([transform(r["features"], prep)[:k] for r in rows])


def predictions(rows, artifact):
    out = {"live": [r["live"] for r in rows], "base": []}
    designs = {name: design(rows, name, artifact) for name in CANDIDATES}
    for name in CANDIDATES:
        out[name] = []
    y = np.array([r["outcome"] for r in rows], dtype=float)
    for i, row in enumerate(rows):
        known = [j for j in range(i) if rows[j]["opens"] <= row["opens"] - timedelta(minutes=30)]
        out["base"].append((y[known].sum() + 1) / (len(known) + 2))
        for name, spec in CANDIDATES.items():
            if len(known) < 100:
                out[name].append(None)
                continue
            X = designs[name][known]
            mu, sd = X.mean(0), X.std(0) + 1e-12
            w = ridge_logistic((X - mu) / sd, y[known], spec["ridge"])
            z = w[0] + ((designs[name][i] - mu) / sd) @ w[1:]
            out[name].append(float(1 / (1 + math.exp(-max(-30, min(30, z))))))
    return out


def trade_pnl(p, row):
    q = row["quote"]
    if q is None or 0.45 < p < 0.55:
        return None
    up = p >= 0.55
    ask = q["up_ask"] if up else q["down_ask"]
    if not 0 < ask < 1:
        return None
    fee = math.ceil(100 * FEE_RATE * ask * (1 - ask)) / 100
    return (1 - ask if (row["outcome"] == 1) == up else -ask) - fee


def bootstrap_ci(values, seed=11, n=4000):
    rng = random.Random(seed)
    values = list(values)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n)]


def report(rows, preds, freeze=FREEZE):
    cut = _ts(freeze)
    idx = [i for i, r in enumerate(rows) if r["opens"] >= cut and all(preds[c][i] is not None for c in CANDIDATES)]
    y = [rows[i]["outcome"] for i in idx]
    out = {"freeze": freeze, "forward_openings": len(idx), "minimum_for_decision": MIN_FORWARD, "models": {}}
    if not idx:
        return out
    live_b = [(preds["live"][i] - rows[i]["outcome"]) ** 2 for i in idx]
    for name in preds:
        b = [(preds[name][i] - rows[i]["outcome"]) ** 2 for i in idx]
        pnl = [x for x in (trade_pnl(preds[name][i], rows[i]) for i in idx) if x is not None]
        diff = [a - c for a, c in zip(b, live_b)]
        calls = [i for i in idx if not 0.45 < preds[name][i] < 0.55]
        correct = sum((preds[name][i] >= 0.5) == (rows[i]["outcome"] == 1) for i in calls)
        out["models"][name] = {
            "brier": sum(b) / len(b),
            "brier_minus_live_ci95": bootstrap_ci(diff) if name != "live" else [0.0, 0.0],
            "calls": len(calls), "call_accuracy": correct / len(calls) if calls else None,
            "trades": len(pnl), "pnl_per_contract": sum(pnl) / len(pnl) if pnl else None,
            "pnl_ci95": bootstrap_ci(pnl) if len(pnl) >= 10 else None,
        }
    passed = []
    for name in CANDIDATES:
        m = out["models"][name]
        # Bonferroni across the frozen candidates: 97.5/len one-sided intervals are approximated by
        # requiring the 95% interval to clear zero for BOTH Brier and after-cost P&L.
        if (len(idx) >= MIN_FORWARD and m["brier_minus_live_ci95"][1] < 0 and m["pnl_ci95"]
                and m["pnl_ci95"][0] > 0):
            passed.append(name)
    out["up_rate"] = sum(y) / len(y)
    out["passed_preregistered_rule"] = passed
    out["decision"] = ("ELIGIBLE FOR A SEPARATE PROMOTION REVIEW" if passed else
                       "NOT ENOUGH FORWARD DATA" if len(idx) < MIN_FORWARD else "NO CANDIDATE PASSED")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--export", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--freeze", default=FREEZE, help="only for smoke tests; the decision uses the frozen value")
    args = ap.parse_args(argv)
    rows = load_rows(json.load(open(args.export)))
    artifact = json.load(open(args.artifact))
    print(json.dumps(report(rows, predictions(rows, artifact), args.freeze), indent=1, default=str))


if __name__ == "__main__":
    main()

"""Opening-model research round v2 (research only; nothing here touches production).

Uses read-only exports of the old (2026-10-30 .. 11-12) and current (2026-11-16 ..) journals.
Only openings BEFORE the frozen forward test (2026-11-20 06:15 UTC) are used, so the
pre-registered evaluation stays uncontaminated.

  python scripts/research_opening_v2.py --old old_dump.json --new audit_dump.json \
      --artifact live_artifact.json --out report.json

Every challenger is refit walk-forward on openings that opened at least 30 minutes earlier
(both settled). The evaluation block starts 2026-11-16 21:15 (the live model's first
out-of-sample opening), so challengers and the live model are scored on the same openings.
Model choices inside each fit use an inner chronological split of the training data only.
"""
import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.signals_display.learning import transform  # noqa: E402  (the live model's own preprocessing)

FREEZE = datetime(2026, 11, 20, 6, 15, tzinfo=timezone.utc)
EVAL_START = datetime(2026, 11, 16, 21, 15, tzinfo=timezone.utc)
REFIT_EVERY = 12
FEE_RATE = 0.07


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def load(paths, freeze=None):
    freeze = FREEZE if freeze is None else freeze
    rows, seen = [], set()
    for path in paths:
        d = json.load(open(path))
        out = {r[0]: r[3] for r in d["forecast_outcomes"]["rows"]}
        quotes = {}
        for r in d.get("opening_quote_snapshots", {}).get("rows", []):
            if r[6] is None or r[8] is None:
                continue
            if r[1] not in quotes or abs(r[4]) < abs(quotes[r[1]][0]):
                quotes[r[1]] = (r[4], r[5], r[6], r[7], r[8])
        for _m, ticker, _g, _c, _cl, up, _s, payload in d["btc_engine_opening"]["rows"]:
            p = json.loads(payload)
            values = (p.get("frozen_features") or {}).get("values") or {}
            o = _ts(p["opens_at"])
            if ticker in seen or not values or out.get(ticker) is None or o >= freeze:
                continue
            seen.add(ticker)
            q = quotes.get(ticker)
            rows.append({"ticker": ticker, "opens": o, "y": int(out[ticker]), "features": values,
                         "live": float(up) if up is not None and str(p.get("model", "")).startswith("opening-candidate") else None,
                         "mid": ((q[1] + q[2]) / 2 + 1 - (q[3] + q[4]) / 2) / 2 if q and None not in q[1:] else None,
                         "up_ask": q[2] if q else None, "down_ask": q[4] if q else None})
    rows.sort(key=lambda r: r["opens"])
    return rows


def ridge_logistic(X, y, lam, iters=60):
    X1 = np.hstack([np.ones((len(X), 1)), X])
    w = np.zeros(X1.shape[1])
    pen = np.full(X1.shape[1], lam)
    pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X1 @ w, -30, 30)))
        g = X1.T @ (p - y) + pen * w
        H = (X1 * (p * (1 - p))[:, None]).T @ X1 + np.diag(pen) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict_lr(w, X):
    return 1 / (1 + np.exp(-np.clip(w[0] + X @ w[1:], -30, 30)))


def brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def logloss(p, y):
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def boot_ci(d, seed=5, n=3000):
    rng = random.Random(seed)
    d = list(d)
    m = sorted(sum(rng.choice(d) for _ in d) / len(d) for _ in range(n))
    return [round(float(m[int(0.025 * n)]), 4), round(float(m[int(0.975 * n)]), 4)]


def calib_slope(p, y):
    lg = np.log(np.clip(p, 1e-6, 1 - 1e-6) / (1 - np.clip(p, 1e-6, 1 - 1e-6))).reshape(-1, 1)
    w = ridge_logistic(lg, np.asarray(y, float), 1e-6)
    return float(w[1])


class Design:
    """Feature matrices built from the frozen as-of-opening values only."""

    def __init__(self, rows, artifact):
        self.all_prep = artifact["model"]["components"][0]["preprocessor"]
        self.core_prep = artifact["model"]["components"][1]["preprocessor"]
        k = len(self.all_prep["names"])
        T = np.array([transform(r["features"], self.all_prep) for r in rows])
        self.values = T[:, :k]                       # standardized by the live model's training stats; missing = 0
        self.flags = T[:, k:2 * k]
        kc = len(self.core_prep["names"])
        self.core = np.array([transform(r["features"], self.core_prep)[:kc] for r in rows])
        self.names = self.all_prep["names"]

        def f(name):
            out = []
            for r in rows:
                v = r["features"].get(name)
                out.append(float(v) if isinstance(v, (int, float)) and math.isfinite(v) else 0.0)
            return np.array(out)
        self.dist = f("distance_scaled")[:, None]
        self.mom = np.column_stack([f("return_15m"), f("return_60m")])


def standardize(Xtr, Xte):
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-12
    return (Xtr - mu) / sd, (Xte - mu) / sd


def inner_choose(X, y, grid):
    """Pick the ridge strength on the last 25% of the training block (chronological), then refit on all."""
    cut = int(len(y) * 0.75)
    best = None
    for lam in grid:
        a, b = standardize(X[:cut], X[cut:])
        s = brier(predict_lr(ridge_logistic(a, y[:cut], lam), b), y[cut:])
        if best is None or s < best[0]:
            best = (s, lam)
    return best[1]


def stable_features(V, F, y, folds=3):
    """Inputs whose univariate direction agrees in every chronological fold of the training block."""
    keep = []
    edges = np.linspace(0, len(y), folds + 1).astype(int)
    for j in range(V.shape[1]):
        signs = []
        for a, b in zip(edges[:-1], edges[1:]):
            x = V[a:b, j]
            present = F[a:b, j] == 0
            if present.sum() < 30 or x[present].std() == 0:
                signs = None
                break
            c = np.corrcoef(x[present], y[a:b][present])[0, 1]
            signs.append(np.sign(c) if abs(c) > 0.03 else 0)
        if signs and len(set(signs)) == 1 and signs[0] != 0:
            keep.append(j)
    return keep


def walk_forward(rows, D, eval_start=None):
    eval_start = EVAL_START if eval_start is None else eval_start
    from sklearn.ensemble import HistGradientBoostingClassifier
    y = np.array([r["y"] for r in rows], float)
    opens = [r["opens"] for r in rows]
    idx = [i for i, r in enumerate(rows) if r["opens"] >= eval_start]
    if not idx:
        return idx, {}, {}, []
    names = ["base", "momentum", "dist", "core16_ridge", "all87_ridge", "stable_ridge", "gbm", "ensemble", "core16_platt"]
    P = {n: np.full(len(rows), np.nan) for n in names}
    fitted = None
    timing = {}
    for t, i in enumerate(idx):
        known = [j for j in range(i) if opens[j] <= opens[i] - timedelta(minutes=30)]
        if fitted is None or t % REFIT_EVERY == 0:
            ytr = y[known]
            fitted = {"base": (ytr.sum() + 1) / (len(ytr) + 2)}
            t0 = time.perf_counter()
            for name, X, grid in (("momentum", D.mom, [1, 10, 100]), ("dist", D.dist, [1, 10]),
                                  ("core16_ridge", D.core, [5, 20, 80, 300]),
                                  ("all87_ridge", np.hstack([D.values, D.flags]), [50, 200, 800, 3000])):
                lam = inner_choose(X[known], ytr, grid)
                mu, sd = X[known].mean(0), X[known].std(0) + 1e-12
                fitted[name] = (ridge_logistic((X[known] - mu) / sd, ytr, lam), mu, sd, X)
            keep = stable_features(D.values[known], D.flags[known], ytr) or [D.names.index("distance_scaled")]
            Xs = D.values[:, keep]
            lam = inner_choose(Xs[known], ytr, [5, 20, 80, 300])
            mu, sd = Xs[known].mean(0), Xs[known].std(0) + 1e-12
            fitted["stable_ridge"] = (ridge_logistic((Xs[known] - mu) / sd, ytr, lam), mu, sd, Xs)
            fitted["stable_keep"] = keep
            g = HistGradientBoostingClassifier(max_depth=2, learning_rate=0.04, max_iter=80, min_samples_leaf=40,
                                               l2_regularization=1.0, random_state=0)
            G = np.hstack([D.values, D.flags])
            fitted["gbm"] = g.fit(G[known], ytr)
            # Platt recalibration of core16 on its own inner split
            cut = int(len(known) * 0.75)
            a = [known[j] for j in range(cut)]
            b = [known[j] for j in range(cut, len(known))]
            lam = inner_choose(D.core[a], y[a], [5, 20, 80, 300])
            mu2, sd2 = D.core[a].mean(0), D.core[a].std(0) + 1e-12
            w2 = ridge_logistic((D.core[a] - mu2) / sd2, y[a], lam)
            pb = predict_lr(w2, (D.core[b] - mu2) / sd2)
            lg = np.log(np.clip(pb, 1e-6, 1 - 1e-6) / (1 - np.clip(pb, 1e-6, 1 - 1e-6))).reshape(-1, 1)
            fitted["platt"] = (ridge_logistic(lg, y[b], 1.0), fitted["core16_ridge"])
            timing["fit_seconds_last"] = round(time.perf_counter() - t0, 3)
        P["base"][i] = fitted["base"]
        for name in ("momentum", "dist", "core16_ridge", "all87_ridge", "stable_ridge"):
            w, mu, sd, X = fitted[name]
            P[name][i] = predict_lr(w, ((X[i] - mu) / sd)[None])[0]
        P["gbm"][i] = fitted["gbm"].predict_proba(np.hstack([D.values, D.flags])[i:i + 1])[0, 1]
        P["ensemble"][i] = np.mean([P["core16_ridge"][i], P["all87_ridge"][i], P["gbm"][i]])
        wpl, (w, mu, sd, X) = fitted["platt"]
        raw = predict_lr(w, ((X[i] - mu) / sd)[None])[0]
        P["core16_platt"][i] = predict_lr(wpl, np.array([[math.log(raw / (1 - raw))]]))[0]
    t0 = time.perf_counter()
    for _ in range(1000):
        predict_lr(fitted["core16_ridge"][0], fitted["core16_ridge"][3][idx[-1]][None])
    timing["core16_inference_ms"] = round((time.perf_counter() - t0), 4)
    return idx, P, timing, fitted.get("stable_keep", [])


def evaluate(rows, idx, P):
    y = np.array([rows[i]["y"] for i in idx], float)
    live = np.array([rows[i]["live"] if rows[i]["live"] is not None else np.nan for i in idx])
    ok = ~np.isnan(live)
    mid = np.array([rows[i]["mid"] if rows[i]["mid"] is not None else np.nan for i in idx])
    allp = dict(P)
    out = {"openings": int(len(idx)), "with_live_probability": int(ok.sum()), "up_rate": float(y.mean()),
           "period": [rows[idx[0]]["opens"].isoformat(), rows[idx[-1]]["opens"].isoformat()], "models": {}}
    live_b = (live - y) ** 2
    vol = np.array([rows[i]["features"].get("volatility_24h") or np.nan for i in idx], float)
    q = np.nanpercentile(vol, [33.3, 66.7])
    regimes = {"low_vol": vol <= q[0], "mid_vol": (vol > q[0]) & (vol <= q[1]), "high_vol": vol > q[1]}
    series = {"live": live, **{k: v[idx] for k, v in allp.items()}}
    mm = ~np.isnan(mid)
    if mm.sum() > 30:
        series["kalshi_mid"] = mid
    for name, p in series.items():
        m = ok & ~np.isnan(p)
        pm, ym = p[m], y[m]
        calls = (pm >= 0.55) | (pm <= 0.45)
        upc, dnc = pm >= 0.55, pm <= 0.45
        res = {"n": int(m.sum()), "brier": brier(pm, ym), "log_loss": logloss(pm, ym),
               "brier_minus_live_ci95": boot_ci((pm - ym) ** 2 - live_b[m]) if name != "live" else [0, 0],
               "calibration_slope": round(calib_slope(pm, ym), 3),
               "calls": int(calls.sum()), "coverage": float(calls.mean()),
               "call_accuracy": float(((pm >= 0.5) == (ym == 1))[calls].mean()) if calls.any() else None,
               "up_call_accuracy": float((ym[upc] == 1).mean()) if upc.any() else None,
               "down_call_accuracy": float((ym[dnc] == 0).mean()) if dnc.any() else None,
               "regime_brier": {k: round(brier(pm[r[m]], ym[r[m]]), 4) for k, r in regimes.items() if r[m].sum() > 10}}
        out["models"][name] = res
    return out


def feature_report(rows, D, until=EVAL_START):
    """Univariate value of each input: chronological folds before the evaluation block, then the block."""
    y = np.array([r["y"] for r in rows], float)
    dev = [i for i, r in enumerate(rows) if r["opens"] < until]
    ev = [i for i, r in enumerate(rows) if r["opens"] >= until]
    edges = np.linspace(0, len(dev), 4).astype(int)
    res = []
    for j, name in enumerate(D.names):
        x, present = D.values[:, j], D.flags[:, j] == 0
        corr = []
        for a, b in list(zip(edges[:-1], edges[1:])) + [(None, None)]:
            sel = np.array(dev[a:b]) if a is not None else np.array(ev)
            s = sel[present[sel]]
            corr.append(float(np.corrcoef(x[s], y[s])[0, 1]) if len(s) >= 30 and x[s].std() > 0 else None)
        miss = float(1 - present[dev + ev].mean())
        res.append({"input": name, "missing_share": round(miss, 3),
                    "corr_by_period": [None if c is None else round(c, 3) for c in corr]})
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    rows = load([a.old, a.new])
    artifact = json.load(open(a.artifact))
    D = Design(rows, artifact)
    idx, P, timing, keep = walk_forward(rows, D)
    report = {"rows_before_freeze": len(rows), "first": rows[0]["opens"].isoformat(), "freeze": FREEZE.isoformat(),
              "evaluation": evaluate(rows, idx, P), "timing": timing,
              "stable_features_last_fit": [D.names[j] for j in keep],
              "features": feature_report(rows, D)}
    Path(a.out).write_text(json.dumps(report, indent=1, default=str))
    ev = report["evaluation"]
    print(f"rows {len(rows)}; evaluation openings {ev['openings']} ({ev['period'][0][:16]} .. {ev['period'][1][:16]}), UP rate {ev['up_rate']:.3f}")
    for k, v in ev["models"].items():
        print(f"{k:14s} n={v['n']:3d} brier {v['brier']:.4f} vs live {v['brier_minus_live_ci95']} logloss {v['log_loss']:.4f} "
              f"slope {v['calibration_slope']:+.2f} calls {v['calls']:3d} acc {v['call_accuracy'] if v['call_accuracy'] is None else round(v['call_accuracy'], 3)} "
              f"UP {v['up_call_accuracy'] if v['up_call_accuracy'] is None else round(v['up_call_accuracy'], 2)} "
              f"DOWN {v['down_call_accuracy'] if v['down_call_accuracy'] is None else round(v['down_call_accuracy'], 2)}")


if __name__ == "__main__":
    main()

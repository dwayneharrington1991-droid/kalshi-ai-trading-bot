"""v5 retraining pipeline on consistently computed inputs (docs/preregistration-v5-retrain-20261122.md).

Modes
  --mode legacy-dry-run   the identical pipeline on legacy (reset-window) openings before 2026-11-20 06:15
                          UTC, which no forward test reserves. Validates the code; its numbers are NOT v5.
  --mode v5               post-fix openings (stream_computation == full-window-v1). Refuses to run before
                          T_TRAIN_END (2026-11-24 06:00 UTC), when tests v1-v4 have decided, so no reserved
                          outcome can be used. Holdout = openings from T_TRAIN_END + 30 min; decision >= 300.

  python scripts/train_v5.py --journal journal_dump.json [--old old_slim.json] --mode legacy-dry-run

Read-only: works on exports; never touches production.
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.signals_display import opening_models as om  # noqa: E402

T_TRAIN_END = datetime(2026, 11, 24, 6, 0, tzinfo=timezone.utc)
HOLDOUT_GAP = timedelta(minutes=30)
MIN_HOLDOUT = 300
LEGACY_END = datetime(2026, 11, 20, 6, 15, tzinfo=timezone.utc)      # v1 freeze: nothing after is used
REFIT_EVERY = 48
LAMBDAS = (10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0, 10000.0)   # amended 11-22 (dry run hit 100 every refit)
COVERAGE = 1 / 3
CANDIDATES = ("post_ridge", "post_gbm", "post_ensemble", "dist")


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ---------------------------------------------------------------- data + quality checks
def load(paths, computation, start=None, end=None):
    """Openings with an official outcome, one per ticker, of a single stream computation."""
    rows, seen, issues = [], set(), {"duplicate_ticker": 0, "no_outcome": 0, "other_computation": 0,
                                     "bad_chronology": 0, "no_features": 0}
    for path in paths:
        d = json.load(open(path))
        out = {r[0]: r[3] for r in d["forecast_outcomes"]["rows"]}
        quotes = {}
        for r in d.get("opening_quote_snapshots", {}).get("rows", []):
            if None in (r[5], r[6], r[7], r[8]):
                continue
            if r[1] not in quotes or abs(r[4]) < abs(quotes[r[1]][0]):
                quotes[r[1]] = (r[4], ((r[5] + r[6]) / 2 + 1 - (r[7] + r[8]) / 2) / 2)
        for _m, ticker, gen, _c, closes, up, _s, payload in d["btc_engine_opening"]["rows"]:
            p = json.loads(payload)
            frozen = p.get("frozen_features") or {}
            values = frozen.get("values") or {}
            opens = _ts(p["opens_at"])
            if (start and opens < start) or (end and opens >= end):
                continue
            if ticker in seen:
                issues["duplicate_ticker"] += 1
                continue
            if not values:
                issues["no_features"] += 1
                continue
            if out.get(ticker) not in (0, 1):
                issues["no_outcome"] += 1
                continue
            if om.stream_computation(frozen, opens) != computation:
                issues["other_computation"] += 1
                continue
            if not (opens <= _ts(gen) < _ts(closes)):
                issues["bad_chronology"] += 1
                continue
            seen.add(ticker)
            rows.append({"ticker": ticker, "opens": opens, "y": int(out[ticker]), "features": values,
                         "live": float(up) if up is not None else None,
                         "mid": quotes.get(ticker, (None, None))[1]})
    rows.sort(key=lambda r: r["opens"])
    return rows, issues


def quality_report(rows, names):
    miss = {n: sum(1 for r in rows if not _finite(r["features"].get(n))) / max(1, len(rows)) for n in names}
    gaps = [(b["opens"] - a["opens"]).total_seconds() / 60 for a, b in zip(rows, rows[1:])]
    return {"openings": len(rows), "up_rate": float(np.mean([r["y"] for r in rows])) if rows else None,
            "inputs_always_missing": sorted(n for n, m in miss.items() if m == 1.0),
            "inputs_missing_over_50pct": sorted(n for n, m in miss.items() if 0.5 < m < 1.0),
            "largest_gap_minutes": max(gaps) if gaps else None}


def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---------------------------------------------------------------- features
def design(rows, names):
    X = np.array([[r["features"].get(n) if _finite(r["features"].get(n)) else np.nan for n in names] for r in rows],
                 float)
    return X


def prepare(Xtr, Xte):
    """Standardise on training rows only; missing -> 0 plus a missing flag; clip to +-8."""
    mu = np.nanmean(Xtr, 0)
    sd = np.nanstd(Xtr, 0)
    keep = np.isfinite(mu) & (sd > 1e-12)
    mu, sd = np.where(keep, mu, 0.0), np.where(keep, sd, 1.0)

    def f(X):
        Z = np.clip((X - mu) / sd, -8, 8)
        M = np.isnan(Z).astype(float)
        Z = np.nan_to_num(Z)
        return np.hstack([Z[:, keep], M[:, keep]])
    return f(Xtr), f(Xte)


def ridge_logistic(X, y, lam, iters=50):
    Xb = np.hstack([X, np.ones((len(X), 1))])
    w = np.zeros(Xb.shape[1])
    reg = np.full(Xb.shape[1], lam)
    reg[-1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Xb @ w, -30, 30)))
        g = Xb.T @ (p - y) + reg * w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + np.diag(reg + 1e-6)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict_lr(w, X):
    return 1 / (1 + np.exp(-np.clip(np.hstack([X, np.ones((len(X), 1))]) @ w, -30, 30)))


def choose_lambda(X, y, folds=5):
    n = len(y)
    best, best_loss = LAMBDAS[0], np.inf
    for lam in LAMBDAS:
        losses = []
        for k in range(1, folds):                      # chronological: train on the past, test the next block
            a, b = int(n * k / folds), int(n * (k + 1) / folds)
            if a < 30 or b <= a:
                continue
            p = predict_lr(ridge_logistic(X[:a], y[:a], lam), X[a:b])
            losses.append(np.mean((p - y[a:b]) ** 2))
        if losses and np.mean(losses) < best_loss:
            best, best_loss = lam, np.mean(losses)
    return best


def _models(Xtr_raw, ytr, Xte_raw, dist_tr, dist_te, lam):
    from sklearn.ensemble import HistGradientBoostingClassifier
    Xtr, Xte = prepare(Xtr_raw, Xte_raw)
    ridge = predict_lr(ridge_logistic(Xtr, ytr, lam), Xte)
    gbm = HistGradientBoostingClassifier(max_depth=2, max_iter=100, learning_rate=0.03, min_samples_leaf=40,
                                         l2_regularization=1.0, random_state=0).fit(np.nan_to_num(Xtr_raw), ytr)
    g = gbm.predict_proba(np.nan_to_num(Xte_raw))[:, 1]
    d = predict_lr(ridge_logistic(dist_tr, ytr, 1.0), dist_te)
    return {"post_ridge": ridge, "post_gbm": g, "post_ensemble": 0.5 * ridge + 0.5 * g, "dist": d}


def fit_predict(Xtr_raw, ytr, Xte_raw, dist_tr, dist_te):
    Xa, _ = prepare(Xtr_raw, Xtr_raw[:1])
    lam = choose_lambda(Xa, ytr)
    preds = _models(Xtr_raw, ytr, Xte_raw, dist_tr, dist_te, lam)
    # Per-model confidence cut for the prespecified 33% coverage, from OUT-OF-SAMPLE predictions on the
    # last 30% of the training rows (models fit on the first 70%) - amended 11-22: in-sample cuts gave
    # 49-68% coverage in the dry run.
    k = int(len(ytr) * 0.7)
    inner = _models(Xtr_raw[:k], ytr[:k], Xtr_raw[k:], dist_tr[:k], dist_tr[k:], lam)
    cuts = {m: float(np.quantile(np.abs(v - 0.5), 1 - COVERAGE)) for m, v in inner.items()}
    return preds, lam, cuts


def walk_forward(rows, names, eval_start):
    X = design(rows, names)
    y = np.array([r["y"] for r in rows], float)
    dist = np.nan_to_num(design(rows, ["distance_scaled"]))
    idx = [i for i, r in enumerate(rows) if r["opens"] >= eval_start]
    P = {k: np.full(len(rows), np.nan) for k in CANDIDATES}
    cuts = {k: np.full(len(rows), np.nan) for k in CANDIDATES}
    lams = []
    for s in range(0, len(idx), REFIT_EVERY):
        block = idx[s:s + REFIT_EVERY]
        first = rows[block[0]]["opens"]
        # only markets that had SETTLED (opens + 15 min) at least HOLDOUT_GAP before the block's first opening
        train = [i for i in range(len(rows)) if rows[i]["opens"] + timedelta(minutes=15) <= first - HOLDOUT_GAP]
        if len(train) < 100:
            continue
        preds, lam, cut = fit_predict(X[train], y[train], X[block], dist[train], dist[block])
        lams.append(lam)
        for k, v in preds.items():
            P[k][block] = v
            cuts[k][block] = cut[k]
    scored = [i for i in idx if not np.isnan(P["post_ensemble"][i])]
    return scored, P, cuts, lams


# ---------------------------------------------------------------- evaluation and gates
def _boot(d, n=3000, seed=5):
    rng = np.random.default_rng(seed)
    m = [d[rng.integers(0, len(d), len(d))].mean() for _ in range(n)]
    return [float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))], float(np.mean(np.array(m) >= 0))


def _wilson(k, n):
    if not n:
        return [None, None]
    z = 1.96
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [c - h, c + h]


def evaluate(rows, scored, P, cuts, decision_minimum):
    y = np.array([rows[i]["y"] for i in scored], float)
    live = np.array([rows[i]["live"] if rows[i]["live"] is not None else np.nan for i in scored])
    mid = np.array([rows[i]["mid"] if rows[i]["mid"] is not None else np.nan for i in scored])
    base = np.array([np.mean([r["y"] for r in rows[:i]]) if i else 0.5 for i in scored])
    out = {"openings": len(scored), "up_rate": float(y.mean()) if len(y) else None,
           "baselines": {}, "candidates": {}}
    for name, p in (("live", live), ("kalshi_mid", mid), ("past_base_rate", base)):
        ok = ~np.isnan(p)
        out["baselines"][name] = {"n": int(ok.sum()), "brier": float(np.mean((p[ok] - y[ok]) ** 2)) if ok.any() else None}
    pvals = {}
    for name in CANDIDATES:
        p = np.array([P[name][i] for i in scored])
        ok = ~np.isnan(live)
        ci, pv = _boot((p[ok] - y[ok]) ** 2 - (live[ok] - y[ok]) ** 2) if ok.sum() > 10 else ([None, None], 1.0)
        pvals[name] = pv
        conf = np.abs(p - 0.5) >= np.array([cuts[name][i] for i in scored])
        k = int(((p[conf] >= 0.5) == (y[conf] == 1)).sum())
        okm = ~np.isnan(mid)
        out["candidates"][name] = {
            "brier": float(np.mean((p - y) ** 2)), "brier_minus_live_ci95": ci,
            "beats_kalshi_mid": bool(okm.any() and np.mean((p[okm] - y[okm]) ** 2) < np.mean((mid[okm] - y[okm]) ** 2)),
            "coverage": float(conf.mean()), "calls": int(conf.sum()), "correct": k,
            "accuracy": k / int(conf.sum()) if conf.sum() else None, "accuracy_ci95": _wilson(k, int(conf.sum())),
            "calibration_slope": _slope(p, y)}
    # Holm across candidates on the one-sided bootstrap p-value of "not better than live"
    order = sorted(pvals, key=pvals.get)
    for rank, name in enumerate(order):
        c = out["candidates"][name]
        holm_ok = all(pvals[n] <= 0.05 / (len(order) - j) for j, n in enumerate(order[:rank + 1]))
        c["passes"] = bool(len(scored) >= decision_minimum and c["brier_minus_live_ci95"][1] is not None
                           and c["brier_minus_live_ci95"][1] < 0 and c["beats_kalshi_mid"] and holm_ok)
    return out


def _slope(p, y):
    z = np.log(np.clip(p, 1e-6, 1 - 1e-6) / np.clip(1 - p, 1e-6, 1))
    try:
        w = ridge_logistic(z[:, None], y, 1e-6)
        return float(w[0])
    except np.linalg.LinAlgError:
        return None


def run(journals, mode, artifact, now=None):
    """Inputs = the live model's 87 receipt-proven inputs (amended 11-22: all 536 candidates overfit)."""
    now = now or datetime.now(timezone.utc)
    names = list(json.load(open(artifact))["model"]["components"][0]["preprocessor"]["names"])
    if mode == "v5":
        if now < T_TRAIN_END + HOLDOUT_GAP:
            raise SystemExit(f"v5 refuses to run before {T_TRAIN_END + HOLDOUT_GAP:%Y-%m-%d %H:%M} UTC: "
                             "post-fix outcomes are reserved for forward tests v1-v4 until then.")
        rows, issues = load(journals, om.STREAM_COMPUTATION, start=om.STREAM_FIX_LIVE_AT)
        eval_start, minimum = T_TRAIN_END + HOLDOUT_GAP, MIN_HOLDOUT
    elif mode == "legacy-dry-run":
        rows, issues = load(journals, om.LEGACY_STREAM_COMPUTATION, end=LEGACY_END)
        eval_start, minimum = datetime(2026, 11, 16, 21, 15, tzinfo=timezone.utc), 300
    else:
        raise SystemExit("unknown mode")
    if not rows:
        return {"mode": mode, "issues": issues, "decision": "NO DATA"}
    scored, P, cuts, lams = walk_forward(rows, names, eval_start)
    report = {"mode": mode, "issues": issues, "quality": quality_report(rows, names),
              "lambdas_chosen": lams, "evaluation": evaluate(rows, scored, P, cuts, minimum)}
    if mode == "legacy-dry-run":
        report["note"] = "Pipeline validation on legacy, non-reserved openings; not a v5 result."
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", action="append", required=True)
    ap.add_argument("--mode", choices=("legacy-dry-run", "v5"), required=True)
    ap.add_argument("--artifact", required=True, help="live artifact; its 87 input names are used")
    a = ap.parse_args(argv)
    print(json.dumps(run(a.journal, a.mode, a.artifact), indent=1, default=str))


if __name__ == "__main__":
    main()

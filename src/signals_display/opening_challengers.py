"""Isolated, read-only walk-forward research: challengers vs the live opening model on identical rows.

Nothing here touches production. It reads the live artifact to reconcile the production input list, uses only
the frozen point-in-time features already stored with each opening, and reports out-of-fold results.

Call rule used for EVERY model, including the live one, so comparisons are like for like:
  UP if P(up) >= 0.55, DOWN if P(up) <= 0.45, otherwise WAIT.
Fold rule: train only on rows whose label was available before the test block's first opening (purged).

These results are exploratory historical out-of-fold results. They are not the unseen forward holdout that
the promotion protocol requires.
"""
import json
import math
from pathlib import Path

CALL_HIGH = 0.55
CALL_LOW = 0.45
L2_GRID = (0.01, 0.1, 1.0)
BLEND_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)


def production_inputs(artifact):
    """Union of the input names the live artifact's components actually use (their prep names)."""
    names = set()

    def walk(node):
        if isinstance(node, dict):
            prep = node.get("prep")
            if isinstance(prep, dict) and isinstance(prep.get("names"), list):
                names.update(prep["names"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(artifact.get("model", artifact))
    return sorted(names)


def _sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _standardize(rows, names):
    means, scales = {}, {}
    for name in names:
        values = [float(r["features"].get(name, 0.0)) for r in rows]
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / max(1, len(values) - 1)
        means[name] = mean
        scales[name] = math.sqrt(var) if var > 1e-12 else 1.0
    return means, scales


def _vector(row, names, means, scales):
    return [max(-5.0, min(5.0, (float(row["features"].get(n, 0.0)) - means[n]) / scales[n])) for n in names]


def _fit(xs, ys, l2, epochs=300, lr=0.2):
    d = len(xs[0])
    w, b, n = [0.0] * d, 0.0, len(xs)
    for _ in range(epochs):
        gw, gb = [0.0] * d, 0.0
        for x, y in zip(xs, ys):
            err = _sigmoid(b + sum(wj * xj for wj, xj in zip(w, x))) - y
            for j in range(d):
                gw[j] += err * x[j]
            gb += err
        w = [wj - lr * (g / n + l2 * wj) for wj, g in zip(w, gw)]
        b -= lr * gb / n
    return w, b


def _predict(model, x):
    w, b = model
    return _sigmoid(b + sum(wj * xj for wj, xj in zip(w, x)))


def _log_loss(ps, ys):
    eps = 1e-12
    return -sum(y * math.log(min(max(p, eps), 1 - eps)) + (1 - y) * math.log(min(max(1 - p, eps), 1 - eps))
                for p, y in zip(ps, ys)) / len(ys)


def fit_logistic(train, names):
    """Fit on train only. L2 is chosen by log loss on a chronological inner validation block (last 25%)."""
    cut = max(1, int(len(train) * 0.75))
    fit_rows, inner_rows = train[:cut], train[cut:] or train[-1:]
    means, scales = _standardize(fit_rows, names)
    fx = [_vector(r, names, means, scales) for r in fit_rows]
    fy = [int(r["outcome"]) for r in fit_rows]
    best = None
    for l2 in L2_GRID:
        model = _fit(fx, fy, l2)
        ps = [_predict(model, _vector(r, names, means, scales)) for r in inner_rows]
        loss = _log_loss(ps, [int(r["outcome"]) for r in inner_rows])
        if best is None or loss < best[0]:
            best = (loss, l2)
    xs = [_vector(r, names, means, scales) for r in train]
    model = _fit(xs, [int(r["outcome"]) for r in train], best[1])
    return {"model": model, "means": means, "scales": scales, "names": names, "l2": best[1]}


def predict_logistic(fitted, rows):
    return [_predict(fitted["model"], _vector(r, fitted["names"], fitted["means"], fitted["scales"])) for r in rows]


def choose_blend(train, names, live_key="recorded_up"):
    """Blend weight between the live probability and the logistic challenger, chosen on the inner validation block."""
    cut = max(1, int(len(train) * 0.75))
    fit_rows, inner = train[:cut], train[cut:] or train[-1:]
    fitted = fit_logistic(fit_rows, names)
    lr_inner = predict_logistic(fitted, inner)
    live_inner = [float(r[live_key]) for r in inner]
    ys = [int(r["outcome"]) for r in inner]
    best = min(BLEND_GRID, key=lambda a: _log_loss([a * l + (1 - a) * p for l, p in zip(live_inner, lr_inner)], ys))
    return best


def call_of(p):
    if p >= CALL_HIGH:
        return "UP"
    if p <= CALL_LOW:
        return "DOWN"
    return "WAIT"


def score(ps, ys):
    """Metrics under the shared call rule. Accuracy is over UP/DOWN calls only."""
    calls = [(call_of(p), y) for p, y in zip(ps, ys)]
    directional = [(c, y) for c, y in calls if c != "WAIT"]
    correct = sum(1 for c, y in directional if (c == "UP") == (y == 1))
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys) if ys else None
    return {
        "eligible": len(ys),
        "calls": len(directional),
        "coverage": round(len(directional) / len(ys), 4) if ys else None,
        "accuracy_on_calls": round(correct / len(directional), 4) if directional else None,
        "brier": round(brier, 5) if brier is not None else None,
        "log_loss": round(_log_loss(ps, ys), 5) if ys else None,
    }


def walk_forward(rows, names, min_train=120, block=30):
    """Expanding-window walk-forward. Returns pooled out-of-fold predictions for every model on identical rows."""
    rows = sorted(rows, key=lambda r: str(r["timestamp"]))
    pooled = {"live": [], "logistic": [], "blend": []}
    ys = []
    start = min_train
    folds = []
    while start < len(rows):
        end = min(start + block, len(rows))
        train = [r for r in rows[:start] if r.get("label_available_at") and
                 str(r["label_available_at"]) <= str(rows[start]["timestamp"])]
        test = rows[start:end]
        if len(train) < min_train or not test:
            start = end
            continue
        fitted = fit_logistic(train, names)
        alpha = choose_blend(train, names)
        lr_p = predict_logistic(fitted, test)
        live_p = [float(r["recorded_up"]) for r in test]
        blend_p = [alpha * l + (1 - alpha) * p for l, p in zip(live_p, lr_p)]
        pooled["live"].extend(live_p)
        pooled["logistic"].extend(lr_p)
        pooled["blend"].extend(blend_p)
        ys.extend(int(r["outcome"]) for r in test)
        folds.append({"test_start": rows[start]["timestamp"], "test_n": len(test), "l2": fitted["l2"], "blend": alpha})
        start = end
    return {
        "folds": folds,
        "results": {name: score(ps, ys) for name, ps in pooled.items()} if ys else {},
    }


def run(snapshot_rows, artifact_path):
    artifact = json.loads(Path(artifact_path).read_text())
    names = production_inputs(artifact)
    observed = sorted({k for r in snapshot_rows for k in r["features"]})
    usable = [n for n in names if any(n in r["features"] for r in snapshot_rows)]
    result = walk_forward(snapshot_rows, usable)
    result["production_inputs_declared"] = len(names)
    result["production_inputs_present_in_rows"] = len(usable)
    result["observed_feature_names"] = len(observed)
    result["call_rule"] = "UP if P>=0.55, DOWN if P<=0.45, else WAIT (identical for all models)"
    result["evaluation_kind"] = "EXPLORATORY_OUT_OF_FOLD_HISTORY_NOT_UNSEEN_HOLDOUT"
    return result


def _main(argv=None):
    import argparse
    import sqlite3
    from datetime import datetime, timezone
    from . import opening_research as research
    ap = argparse.ArgumentParser(description="read-only walk-forward challenger research on a journal snapshot")
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--artifact", type=Path, required=True)
    ap.add_argument("--asof", required=True)
    args = ap.parse_args(argv)
    asof = datetime.fromisoformat(args.asof.replace("Z", "+00:00"))
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc)
    with sqlite3.connect(f"file:{args.snapshot.resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        rows = research.load(db, asof)
    print(json.dumps(run(rows, args.artifact), indent=1, sort_keys=True, default=str))


if __name__ == "__main__":
    _main()

"""Isolated, read-only walk-forward research: challengers vs the live opening model on identical rows.

Production inputs come from the verified artifact path artifact.model.components[*].preprocessor.names
(87 names in the live artifact). Nothing here touches production.

Call rule used for EVERY model, including the live one: UP if P(up) >= 0.55, DOWN if P(up) <= 0.45, else WAIT.
Fold rule: train only on rows whose label was available before the test block's first opening (purged).

Results are exploratory out-of-fold historical results, not the unseen forward holdout required for promotion.
"""
import json
import math
from pathlib import Path

CALL_HIGH = 0.55
CALL_LOW = 0.45
L2_GRID = (0.01, 0.1, 1.0)
BLEND_GRID = (0.0, 0.25, 0.5, 0.75, 1.0)
Z_95 = 1.96


def production_inputs(artifact):
    """Union of preprocessor names across the artifact's components: the model's actual input contract."""
    model = artifact.get("model", artifact)
    names = set()
    for component in model.get("components") or []:
        prep = component.get("preprocessor") if isinstance(component, dict) else None
        if isinstance(prep, dict) and isinstance(prep.get("names"), list):
            names.update(prep["names"])
    return sorted(names)


def component_input_counts(artifact):
    model = artifact.get("model", artifact)
    return [len((c.get("preprocessor") or {}).get("names") or []) for c in model.get("components") or []
            if isinstance(c, dict)]


def _sigmoid(z):
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


def _logit(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


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


def _raw_predict(model, x):
    w, b = model
    return _sigmoid(b + sum(wj * xj for wj, xj in zip(w, x)))


def _log_loss_one(p, y):
    eps = 1e-12
    return -(y * math.log(min(max(p, eps), 1 - eps)) + (1 - y) * math.log(min(max(1 - p, eps), 1 - eps)))


def _log_loss(ps, ys):
    return sum(_log_loss_one(p, y) for p, y in zip(ps, ys)) / len(ys)


def _fit_platt(logits, ys, epochs=400, lr=0.1):
    """Platt scaling: fit a, b so that sigmoid(a*logit + b) matches outcomes on a held-out block."""
    a, b = 1.0, 0.0
    n = len(logits)
    for _ in range(epochs):
        ga, gb = 0.0, 0.0
        for z, y in zip(logits, ys):
            err = _sigmoid(a * z + b) - y
            ga += err * z
            gb += err
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def _split_inner(train):
    cut = max(1, int(len(train) * 0.75))
    return train[:cut], train[cut:] or train[-1:]


def fit_logistic(train, names):
    """Feature-based logistic. L2 chosen by log loss on a chronological inner block; calibrated via Platt on it."""
    fit_rows, inner = _split_inner(train)
    means, scales = _standardize(fit_rows, names)
    fx = [_vector(r, names, means, scales) for r in fit_rows]
    fy = [int(r["outcome"]) for r in fit_rows]
    inner_x = [_vector(r, names, means, scales) for r in inner]
    inner_y = [int(r["outcome"]) for r in inner]
    best = None
    for l2 in L2_GRID:
        model = _fit(fx, fy, l2)
        loss = _log_loss([_raw_predict(model, x) for x in inner_x], inner_y)
        if best is None or loss < best[0]:
            best = (loss, l2)
    inner_model = _fit(fx, fy, best[1])
    platt = _fit_platt([_logit(_raw_predict(inner_model, x)) for x in inner_x], inner_y)
    xs = [_vector(r, names, means, scales) for r in train]
    model = _fit(xs, [int(r["outcome"]) for r in train], best[1])
    return {"model": model, "means": means, "scales": scales, "names": names, "l2": best[1], "platt": platt}


def predict_logistic(fitted, rows, calibrated=False):
    out = []
    for r in rows:
        x = _vector(r, fitted["names"], fitted["means"], fitted["scales"])
        p = _raw_predict(fitted["model"], x)
        if calibrated:
            a, b = fitted["platt"]
            p = _sigmoid(a * _logit(p) + b)
        out.append(p)
    return out


def choose_blend(train, names):
    """Blend weight between live probability and the logistic challenger, chosen on the inner block only."""
    fit_rows, inner = _split_inner(train)
    fitted = fit_logistic(fit_rows, names)
    lr_inner = predict_logistic(fitted, inner)
    live_inner = [float(r["recorded_up"]) for r in inner]
    ys = [int(r["outcome"]) for r in inner]
    return min(BLEND_GRID, key=lambda a: _log_loss([a * l + (1 - a) * p for l, p in zip(live_inner, lr_inner)], ys))


def call_of(p):
    if p >= CALL_HIGH:
        return "UP"
    if p <= CALL_LOW:
        return "DOWN"
    return "WAIT"


def wilson(correct, n):
    if n == 0:
        return None
    p = correct / n
    denom = 1 + Z_95 ** 2 / n
    centre = (p + Z_95 ** 2 / (2 * n)) / denom
    half = Z_95 * math.sqrt(p * (1 - p) / n + Z_95 ** 2 / (4 * n * n)) / denom
    return [round(centre - half, 4), round(centre + half, 4)]


def _ece(ps, ys, bins=5):
    total, err = len(ys), 0.0
    for k in range(bins):
        idx = [i for i, p in enumerate(ps) if k / bins <= p < (k + 1) / bins or (k == bins - 1 and p == 1.0)]
        if idx:
            err += len(idx) / total * abs(sum(ps[i] for i in idx) / len(idx) - sum(ys[i] for i in idx) / len(idx))
    return err


def score(ps, ys):
    directional = [(p, y) for p, y in zip(ps, ys) if call_of(p) != "WAIT"]
    correct = sum(1 for p, y in directional if (call_of(p) == "UP") == (y == 1))
    brier = sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys) if ys else None
    return {
        "eligible": len(ys),
        "calls": len(directional),
        "correct": correct,
        "coverage": round(len(directional) / len(ys), 4) if ys else None,
        "accuracy_on_calls": round(correct / len(directional), 4) if directional else None,
        "accuracy_wilson_95": wilson(correct, len(directional)),
        "brier": round(brier, 5) if brier is not None else None,
        "log_loss": round(_log_loss(ps, ys), 5) if ys else None,
        "calibration_ece": round(_ece(ps, ys), 5) if ys else None,
    }


def paired_logloss_gain(live_ps, model_ps, ys):
    """Per-opening log-loss gain of the model over the live model (positive = model better), with a z statistic."""
    diffs = [_log_loss_one(l, y) - _log_loss_one(m, y) for l, m, y in zip(live_ps, model_ps, ys)]
    n = len(diffs)
    mean = sum(diffs) / n
    var = sum((d - mean) ** 2 for d in diffs) / (n - 1) if n > 1 else 0.0
    z = mean / math.sqrt(var / n) if var > 0 else 0.0
    return {"mean_gain": round(mean, 5), "z": round(z, 3), "n": n}


def _regime(row):
    return str(row.get("regime") or "unknown").split("_")[0]


def by_regime(rows, ps_by_model, ys):
    out = {}
    for regime in sorted({_regime(r) for r in rows}):
        idx = [i for i, r in enumerate(rows) if _regime(r) == regime]
        out[regime] = {"openings": len(idx)}
        for name, ps in ps_by_model.items():
            s = score([ps[i] for i in idx], [ys[i] for i in idx])
            out[regime][name] = {"calls": s["calls"], "accuracy_on_calls": s["accuracy_on_calls"],
                                 "log_loss": s["log_loss"]}
    return out


def walk_forward(rows, names, min_train=120, block=30):
    """Expanding-window walk-forward with purging. Every model is scored on the same pooled test openings."""
    rows = sorted(rows, key=lambda r: str(r["timestamp"]))
    pooled_rows = []
    pooled = {"live": [], "logistic": [], "logistic_calibrated": [], "blend": []}
    ys = []
    folds = []
    start = min_train
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
        live_p = [float(r["recorded_up"]) for r in test]
        lr_p = predict_logistic(fitted, test)
        cal_p = predict_logistic(fitted, test, calibrated=True)
        pooled["live"].extend(live_p)
        pooled["logistic"].extend(lr_p)
        pooled["logistic_calibrated"].extend(cal_p)
        pooled["blend"].extend(alpha * l + (1 - alpha) * p for l, p in zip(live_p, lr_p))
        pooled_rows.extend(test)
        ys.extend(int(r["outcome"]) for r in test)
        folds.append({"test_start": rows[start]["timestamp"], "test_n": len(test), "l2": fitted["l2"], "blend": alpha})
        start = end
    if not ys:
        return {"folds": [], "results": {}, "by_regime": {}, "paired_vs_live": {}}
    results = {name: score(ps, ys) for name, ps in pooled.items()}
    paired = {name: paired_logloss_gain(pooled["live"], ps, ys) for name, ps in pooled.items() if name != "live"}
    return {"folds": folds, "results": results, "paired_vs_live": paired,
            "by_regime": by_regime(pooled_rows, pooled, ys)}


def run(snapshot_rows, artifact_path):
    artifact = json.loads(Path(artifact_path).read_text())
    names = production_inputs(artifact)
    present = [n for n in names if any(n in r["features"] for r in snapshot_rows)]
    result = walk_forward(snapshot_rows, present)
    result["production_inputs_declared"] = len(names)
    result["production_inputs_present_in_rows"] = len(present)
    result["component_input_counts"] = component_input_counts(artifact)
    result["call_rule"] = "UP if P>=0.55, DOWN if P<=0.45, else WAIT (identical for all models)"
    result["evaluation_kind"] = "EXPLORATORY_OUT_OF_FOLD_HISTORY_NOT_UNSEEN_HOLDOUT"
    return result


def _main(argv=None):
    import argparse
    import sqlite3
    from datetime import timezone
    from datetime import datetime
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

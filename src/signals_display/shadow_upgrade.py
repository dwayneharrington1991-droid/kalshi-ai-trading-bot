"""Shadow-only 8768 upgrade research: nonlinear and ensemble opening models.

SHADOW ONLY. Nothing here is imported by the live bot, writes to a journal,
artifact or registry, or has any activation path. The report format is
deliberately not a forecast artifact.

What this adds beside ``opening_research`` (which it reuses for loading,
purging and scoring) is:

* a numpy gradient-boosted decision-tree model (depth-limited, shrinkage,
  L2-regularised leaves, early stopping on a purged chronological tail of the
  training block) over every frozen as-of-opening feature family;
* a wide regularised logistic model over the same families;
* a Platt-calibrated ensemble of the two;
* an accuracy-target frontier: for 70/80/85/90% the confidence threshold is
  chosen on development (walk-forward out-of-sample) predictions only, and the
  untouched sealed holdout is then scored ONCE, reporting accuracy, Wilson 95%
  interval and the share of markets that received an UP/DOWN call;
* the running AI's displayed calls on the identical holdout contracts;
* per-regime accuracy and measured inference latency.

Pre-registered before any data is read: the three candidates below, the
threshold grid, the accuracy targets, the 20% sealed holdout, and the rule
that ONE candidate (lowest development log loss) is the selected model.
Results for the other candidates are labelled exploratory.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from . import learning, opening_models, opening_research as research, research_fast as fast, rigorous_evaluation as rigor
from .calibration import fit_platt
from .model import timestamp
from .scores import score, wilson_interval

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a declared dependency
    np = None

REPORT_FORMAT = "kxbtc-shadow-upgrade-8768-report-v1"
SEALED_HOLDOUT_FRACTION = 0.20
ACCURACY_TARGETS = (0.70, 0.80, 0.85, 0.90)
THRESHOLD_GRID = tuple(round(0.50 + 0.01 * i, 2) for i in range(46))  # 0.50 .. 0.95
MIN_DEVELOPMENT_CALLS = 30
GBDT = dict(max_rounds=200, learning_rate=0.05, depth=3, min_leaf=20, l2=5.0, bins=32, subsample=0.8,
            patience=25, seed=20261118)

ALL_FEATURES = tuple(dict.fromkeys(
    rigor.COMPACT_FEATURES
    + tuple(f for group in research.FEATURE_GROUPS.values() for f in group)
    + ("regime_rising", "regime_falling") + tuple(research.INTERACTIONS)
))
CANDIDATES = ("logistic_all_families", "gbdt_depth3_all_families", "calibrated_ensemble")


# ----------------------------------------------------------------- GBDT core


def _bin_edges(column, bins):
    finite = column[np.isfinite(column)]
    if finite.size == 0:
        return np.array([])
    edges = np.unique(np.quantile(finite, np.linspace(0, 1, bins + 1)[1:-1]))
    return edges


def _binned(X, edges):
    return np.stack([np.searchsorted(e, X[:, j], side="right") if e.size else np.zeros(len(X), int)
                     for j, e in enumerate(edges)], axis=1)


def _best_split(B, g, h, idx, nbins, min_leaf, l2):
    G, H = g[idx].sum(), h[idx].sum()
    parent = G * G / (H + l2)
    best = None
    count_all = len(idx)
    for j in range(B.shape[1]):
        nb = nbins[j]
        if nb < 2:
            continue
        col = B[idx, j]
        gs = np.bincount(col, weights=g[idx], minlength=nb)
        hs = np.bincount(col, weights=h[idx], minlength=nb)
        cs = np.bincount(col, minlength=nb)
        gl, hl, cl = np.cumsum(gs)[:-1], np.cumsum(hs)[:-1], np.cumsum(cs)[:-1]
        ok = (cl >= min_leaf) & (count_all - cl >= min_leaf)
        if not ok.any():
            continue
        gain = gl * gl / (hl + l2) + (G - gl) ** 2 / (H - hl + l2) - parent
        gain = np.where(ok, gain, -np.inf)
        k = int(np.argmax(gain))
        if gain[k] > 1e-9 and (best is None or gain[k] > best[0]):
            best = (float(gain[k]), j, k)
    return best, G, H


def _grow(B, g, h, idx, depth, nbins, edges, P):
    best, G, H = _best_split(B, g, h, idx, nbins, P["min_leaf"], P["l2"])
    if depth == 0 or best is None:
        return {"leaf": float(-G / (H + P["l2"]) * P["learning_rate"])}
    _, j, k = best
    left = idx[B[idx, j] <= k]
    right = idx[B[idx, j] > k]
    return {"feature": j, "threshold": float(edges[j][k]),
            "left": _grow(B, g, h, left, depth - 1, nbins, edges, P),
            "right": _grow(B, g, h, right, depth - 1, nbins, edges, P)}


def _tree_value(tree, x):
    while "leaf" not in tree:
        tree = tree["left"] if x[tree["feature"]] <= tree["threshold"] else tree["right"]
    return tree["leaf"]


def _tree_values(tree, X):
    out = np.empty(len(X))
    for i in range(len(X)):
        out[i] = _tree_value(tree, X[i])
    return out


def _gbdt_rounds(X, y, base, edges, P, rounds, eval_X=None, eval_y=None):
    """Boost up to ``rounds`` trees; with an eval set, return the best count."""
    B = _binned(X, edges)
    nbins = [e.size + 1 for e in edges]
    rng = np.random.default_rng(P["seed"])
    F = np.full(len(X), base)
    E = np.full(len(eval_X), base) if eval_X is not None else None
    trees, losses = [], []
    for _ in range(rounds):
        p = 1 / (1 + np.exp(-F))
        g, h = p - y, np.maximum(p * (1 - p), 1e-6)
        idx = np.flatnonzero(rng.random(len(X)) < P["subsample"])
        if len(idx) < 2 * P["min_leaf"]:
            idx = np.arange(len(X))
        tree = _grow(B, g, h, idx, P["depth"], nbins, edges, P)
        trees.append(tree)
        F += _tree_values(tree, X)
        if E is not None:
            E += _tree_values(tree, eval_X)
            q = np.clip(1 / (1 + np.exp(-E)), 1e-6, 1 - 1e-6)
            losses.append(float(-np.mean(eval_y * np.log(q) + (1 - eval_y) * np.log(1 - q))))
            best = int(np.argmin(losses))
            if len(losses) - 1 - best >= P["patience"]:
                break
    if E is not None:
        return trees, int(np.argmin(losses)) + 1
    return trees, len(trees)


def fit_gbdt(train, prep, params=None):
    """Gradient-boosted trees on training rows only. The round count is chosen
    on the last 20% of the training block (purged by label time), then the
    model is refitted on the whole training block with that round count."""
    if np is None:
        raise ValueError("numpy unavailable")
    P = {**GBDT, **(params or {})}
    X = np.array([learning.transform(r["features"], prep) for r in train], float)
    y = np.array([r["outcome"] for r in train], float)
    if len(train) < 4 * P["min_leaf"] or len(set(y.tolist())) < 2:
        raise ValueError("not enough training rows for boosting")
    edges = [_bin_edges(X[:, j], P["bins"]) for j in range(X.shape[1])]
    cut = int(len(train) * 0.8)
    stop_start = min(timestamp(r["timestamp"]) for r in train[cut:])
    fit_idx = [i for i in range(cut) if timestamp(train[i]["label_available_at"]) <= stop_start]
    base = math.log((y.sum() + 1) / (len(y) - y.sum() + 1))
    rounds = P["max_rounds"]
    if len(fit_idx) >= 4 * P["min_leaf"] and len(train) - cut >= 20:
        _, rounds = _gbdt_rounds(X[fit_idx], y[fit_idx], base, edges, P, P["max_rounds"],
                                 X[cut:], y[cut:])
    trees, _ = _gbdt_rounds(X, y, base, edges, P, rounds)
    return {"kind": "gbdt", "base": base, "trees": trees, "preprocessor": prep, "rounds": len(trees)}


def gbdt_raw(model, features):
    x = learning.transform(features, model["preprocessor"])
    return learning.sigmoid(model["base"] + sum(_tree_value(t, x) for t in model["trees"]))


# ------------------------------------------------------------ candidate fits


def _fit_logistic(train):
    spec = dict(features=ALL_FEATURES, offset=True)
    names = [n for n in ALL_FEATURES if any(r["features"].get(n) is not None for r in train)]
    prep = research._preprocess(train, names)
    if not prep["names"]:
        raise ValueError("no usable features")
    best, size = None, len(train) // 4
    for l2 in research.RESEARCH_LAMBDA_GRID:  # expanding CV inside train only
        losses = []
        for fold in range(1, 4):
            fit_rows, held = train[: fold * size], train[fold * size: (fold + 1) * size]
            if not held:
                continue
            start = min(timestamp(r["timestamp"]) for r in held)
            fit_rows = [r for r in fit_rows if timestamp(r["label_available_at"]) <= start]
            fold_prep = research._preprocess(fit_rows, names) if len(fit_rows) >= 20 else None
            if not fold_prep or not fold_prep["names"]:
                continue
            m = fast.fit_logistic(fit_rows, fold_prep, l2, use_offset=spec["offset"])
            losses += rigor._log_losses([rigor.raw_probability(m, r) for r in held], [r["outcome"] for r in held])
        if losses and (best is None or sum(losses) / len(losses) < best[0]):
            best = (sum(losses) / len(losses), l2)
    model = fast.fit_logistic(train, prep, best[1] if best else 30.0, use_offset=True)
    return lambda row: rigor.raw_probability(model, row)


def _fit_gbdt(train):
    names = [n for n in ALL_FEATURES if any(r["features"].get(n) is not None for r in train)]
    prep = research._preprocess(train, names)
    if not prep["names"]:
        raise ValueError("no usable features")
    model = fit_gbdt(train, prep)
    return lambda row: gbdt_raw(model, row["features"])


def _calibrated(raw, validation):
    platt = fit_platt([raw(r) for r in validation], [r["outcome"] for r in validation])
    return lambda row: learning.sigmoid(platt["slope"] * learning.logit(raw(row)) + platt["intercept"])


def fit_candidates(train, validation):
    """{candidate: predict(row) -> P(up)}; every fitted value sees train only,
    every calibration sees validation only."""
    out, raws = {}, {}
    for name, fitter in (("logistic_all_families", _fit_logistic), ("gbdt_depth3_all_families", _fit_gbdt)):
        try:
            raws[name] = fitter(train)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            continue
        out[name] = _calibrated(raws[name], validation)
    if len(raws) == 2:
        a, b = raws["logistic_all_families"], raws["gbdt_depth3_all_families"]
        mean = lambda row: learning.sigmoid(0.5 * (learning.logit(a(row)) + learning.logit(b(row))))
        out["calibrated_ensemble"] = _calibrated(mean, validation)
    return out


# ------------------------------------------------------------ walk-forward


def walk_forward(rows, progress=None):
    """Expanding chronological folds on development rows; then every candidate
    is fitted ONCE on development rows and frozen to score the sealed holdout.
    Returns (rows ordered, seal index, {candidate: {ticker: p}}, folds)."""
    rows = opening_models._validate_rows(rows)
    seal = int(len(rows) * (1 - SEALED_HOLDOUT_FRACTION))
    start = research.WALK_START
    block = max(research.WALK_BLOCK, math.ceil(max(0, seal - start) / research.MAX_FOLDS))
    predictions = {name: {} for name in CANDIDATES}
    folds = 0
    ends = list(range(start, seal, block)) + ([seal] if seal < len(rows) else [])
    for end in ends:
        sealed = end == seal
        stop = len(rows) if sealed else min(end + block, seal)
        past, test = rows[max(0, end - research.MAX_TRAIN_ROWS):end], rows[end:stop]
        split = int(len(past) * 0.8)
        if not test or split == len(past):
            continue
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 50 or len(validation) < 15 or not test:
            continue
        if min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10:
            continue
        folds += 1
        t0 = time.monotonic()
        for name, predict in fit_candidates(train, validation).items():
            predictions[name].update({r["ticker"]: predict(r) for r in test})
        if progress:
            progress(f"{'SEALED HOLDOUT' if sealed else f'fold {folds}'}: train<= {end}, tested {len(test)} "
                     f"in {time.monotonic() - t0:.1f}s")
    return rows, seal, predictions, folds


# ------------------------------------------------------------- measurement


def _metrics(pairs, total):
    n = len(pairs)
    correct = sum((p >= 0.5) == bool(r["outcome"]) for p, r in pairs)
    probabilistic = score([p for p, _ in pairs], [r["outcome"] for _, r in pairs]) if pairs else {}
    return {"calls": n, "correct": correct, "wrong": n - correct,
            "accuracy": correct / n if n else None,
            "accuracy_wilson_95": wilson_interval(correct, n) if n else None,
            "markets": total, "coverage": n / total if total else 0.0,
            "up_calls": sum(p >= 0.5 for p, _ in pairs), "down_calls": sum(p < 0.5 for p, _ in pairs),
            "brier": probabilistic.get("brier"), "log_loss": probabilistic.get("log_loss")}


def _called(by_ticker, rows, threshold):
    return [(by_ticker[r["ticker"]], r) for r in rows
            if r["ticker"] in by_ticker and max(by_ticker[r["ticker"]], 1 - by_ticker[r["ticker"]]) >= threshold]


def frontier(by_ticker, development, holdout):
    """For each accuracy target, the lowest threshold whose DEVELOPMENT accuracy
    reaches it with >= MIN_DEVELOPMENT_CALLS calls (maximum coverage); the
    holdout is then scored once at that frozen threshold."""
    dev = [r for r in development if r["ticker"] in by_ticker]
    out = {}
    for target in ACCURACY_TARGETS:
        chosen = None
        for threshold in THRESHOLD_GRID:
            m = _metrics(_called(by_ticker, dev, threshold), len(dev))
            if m["calls"] >= MIN_DEVELOPMENT_CALLS and m["accuracy"] >= target:
                chosen = (threshold, m)
                break
        key = f"{int(target * 100)}%"
        if chosen is None:
            out[key] = {"status": "NOT_REACHED_IN_DEVELOPMENT"}
            continue
        threshold, dm = chosen
        hm = _metrics(_called(by_ticker, holdout, threshold), len(holdout))
        lower = (hm["accuracy_wilson_95"] or [0, 0])[0]
        out[key] = {
            "threshold": threshold,
            "development": {k: dm[k] for k in ("calls", "accuracy", "coverage")},
            "holdout": hm,
            "status": ("VERIFIED_WILSON_LOWER_BOUND_AT_TARGET" if hm["calls"] and lower >= target
                       else "HOLDOUT_POINT_ESTIMATE_AT_TARGET" if hm["calls"] and hm["accuracy"] >= target
                       else "NOT_CONFIRMED_ON_HOLDOUT"),
        }
    return out


def current_ai(holdout):
    """The running AI's immutable displayed UP/DOWN calls on the same holdout."""
    pairs = []
    for r in holdout:
        p = r.get("recorded_up")
        if (r.get("recorded_direction") in ("UP", "DOWN")
                and r.get("recorded_confidence_status") in research.DISPLAYABLE_RECORDED_CONFIDENCE
                and type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1
                and (r["recorded_direction"] == "UP") == (p >= 0.5)):
            pairs.append((p, r))
    recorded = [r for r in holdout if type(r.get("recorded_up")) in (int, float)]
    out = _metrics(pairs, len(holdout))
    out["all_recorded_probabilities"] = _metrics([(r["recorded_up"], r) for r in recorded], len(holdout))
    return out, pairs


def regimes(by_ticker, development, holdout):
    """Accuracy of every-market directional calls (p >= 0.5 is UP) by condition.
    Volatility terciles are cut on development values only."""
    vols = sorted(v for r in development if type(v := r["features"].get("volatility_24h")) in (int, float))
    cuts = (vols[len(vols) // 3], vols[2 * len(vols) // 3]) if len(vols) >= 30 else None
    groups = {}
    for r in holdout:
        if r["ticker"] not in by_ticker:
            continue
        f = r["features"]
        v, gap = f.get("volatility_24h"), f.get("opening_ema9_21_gap")
        labels = []
        if cuts and type(v) in (int, float):
            labels.append("volatility_" + ("low" if v <= cuts[0] else "mid" if v <= cuts[1] else "high"))
        if type(gap) in (int, float):
            labels.append("trend_up" if gap > 0 else "trend_down")
        labels.append("regime_" + str(r.get("regime") or "unknown").split()[0])
        for label in labels:
            groups.setdefault(label, []).append((by_ticker[r["ticker"]], r))
    return {k: _metrics(v, len(v)) for k, v in sorted(groups.items())}


def latency(rows, development):
    """Fit once on development rows; time single-row inference like the live path."""
    out = {}
    split = int(len(development) * 0.8)
    if split < 50 or not rows:
        return out
    train, validation, _ = opening_models._purge(development[:split], development[split:], rows[:1])
    t0 = time.monotonic()
    fitted = fit_candidates(train, validation)
    out["fit_seconds_all_candidates"] = round(time.monotonic() - t0, 3)
    sample = rows[: min(len(rows), 300)]
    for name, predict in fitted.items():
        times = []
        for r in sample:
            t = time.perf_counter()
            predict(r)
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        out[name] = {"p50_ms": round(times[len(times) // 2], 4), "p99_ms": round(times[int(len(times) * 0.99) - 1], 4),
                     "samples": len(times)}
    return out


def evaluate(rows, progress=None):
    research.add_interactions(rows)
    ordered, seal, predictions, folds = walk_forward(rows, progress)
    development, holdout = ordered[:seal], ordered[seal:]
    report = {"format": REPORT_FORMAT, "shadow_only": True, "activation_path": None,
              "contracts": len(ordered), "development_contracts": len(development),
              "holdout_contracts": len(holdout), "folds": folds,
              "first_contract_close": ordered[0]["closes_at"] if ordered else None,
              "holdout_starts": holdout[0]["closes_at"] if holdout else None,
              "last_contract_close": ordered[-1]["closes_at"] if ordered else None,
              "preregistered": {"candidates": CANDIDATES, "accuracy_targets": ACCURACY_TARGETS,
                                "threshold_grid": [THRESHOLD_GRID[0], THRESHOLD_GRID[-1], 0.01],
                                "min_development_calls": MIN_DEVELOPMENT_CALLS,
                                "sealed_holdout_fraction": SEALED_HOLDOUT_FRACTION, "gbdt": GBDT}}
    if not holdout or folds == 0:
        report["status"] = "INSUFFICIENT_DATA"
        return report
    dev_scores = {}
    for name, by in predictions.items():
        dev_pairs = [(by[r["ticker"]], r) for r in development if r["ticker"] in by]
        if len(dev_pairs) >= MIN_DEVELOPMENT_CALLS:
            dev_scores[name] = score([p for p, _ in dev_pairs], [r["outcome"] for _, r in dev_pairs])["log_loss"]
    selected = min(dev_scores, key=dev_scores.get) if dev_scores else None
    current, current_pairs = current_ai(holdout)
    report["current_ai_holdout"] = current
    report["selected_candidate"] = selected
    report["development_log_loss"] = dev_scores
    report["candidates"] = {}
    for name, by in predictions.items():
        if not by:
            continue
        entry = {
            "role": "SELECTED (pre-registered rule: lowest development log loss)" if name == selected else "exploratory",
            "holdout_every_market": _metrics([(by[r["ticker"]], r) for r in holdout if r["ticker"] in by], len(holdout)),
            "accuracy_frontier": frontier(by, development, holdout),
        }
        same = [(by[r["ticker"]], r) for _, r in current_pairs if r["ticker"] in by]
        if same:
            entry["same_contracts_as_current_ai_calls"] = _metrics(same, len(same))
            ranked = sorted((r for r in holdout if r["ticker"] in by), key=lambda r: -abs(by[r["ticker"]] - 0.5))
            entry["matched_coverage_vs_current_ai"] = _metrics([(by[r["ticker"]], r) for r in ranked[:len(current_pairs)]], len(holdout))
        if name == selected:
            entry["regimes"] = regimes(by, development, holdout)
        report["candidates"][name] = entry
    report["latency"] = latency(holdout, development)
    report["status"] = "MEASURED"
    return report


def summary(report):
    lines = [f"SHADOW UPGRADE 8768 ({report['format']}) status {report.get('status')}",
             f"contracts {report['contracts']} (development {report['development_contracts']}, sealed holdout "
             f"{report['holdout_contracts']}), folds {report['folds']}"]
    if report.get("status") != "MEASURED":
        return "\n".join(lines)
    pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"
    ci = lambda m: "n/a" if not m.get("accuracy_wilson_95") else "-".join(pct(v) for v in m["accuracy_wilson_95"])
    c = report["current_ai_holdout"]
    lines.append(f"CURRENT AI (displayed calls, holdout): {c['correct']}/{c['calls']} = {pct(c['accuracy'])} "
                 f"[{ci(c)}], coverage {pct(c['coverage'])}")
    for name, e in report["candidates"].items():
        m = e["holdout_every_market"]
        lines.append(f"{name} [{e['role'].split(' ')[0]}]: every market {m['correct']}/{m['calls']} = {pct(m['accuracy'])} "
                     f"[{ci(m)}] brier {m['brier']:.4f} logloss {m['log_loss']:.4f}")
        for target, f in e["accuracy_frontier"].items():
            if "holdout" not in f:
                lines.append(f"   target {target}: {f['status']}")
                continue
            h = f["holdout"]
            lines.append(f"   target {target}: threshold {f['threshold']:.2f} -> holdout {h['correct']}/{h['calls']} = "
                         f"{pct(h['accuracy'])} [{ci(h)}], coverage {pct(h['coverage'])} ({f['status']})")
        if "same_contracts_as_current_ai_calls" in e:
            s, k = e["same_contracts_as_current_ai_calls"], e["matched_coverage_vs_current_ai"]
            lines.append(f"   on the current AI's called contracts: {s['correct']}/{s['calls']} = {pct(s['accuracy'])}; "
                         f"matched coverage: {k['correct']}/{k['calls']} = {pct(k['accuracy'])}")
    for name, lat in (report.get("latency") or {}).items():
        if isinstance(lat, dict):
            lines.append(f"latency {name}: p50 {lat['p50_ms']} ms, p99 {lat['p99_ms']} ms per prediction")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asof", help="ISO-8601 cutoff; only labels observed by then are used")
    args = parser.parse_args(argv)
    out = args.output.resolve()
    if out.suffix != ".json" or out == args.journal.resolve() or out.name.startswith(args.journal.name):
        parser.error("output must be a separate .json report, never beside-named or equal to the journal")
    asof = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    with closing(sqlite3.connect(f"file:{args.journal.resolve()}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        t0 = time.monotonic()
        rows = research.load(db, asof)
        load_seconds = time.monotonic() - t0
    report = evaluate(rows, progress=lambda m: print("SHADOW:", m, flush=True))
    report["asof"] = asof.isoformat()
    report["load_seconds"] = round(load_seconds, 2)
    tmp = out.with_suffix(".json.next")
    tmp.write_text(json.dumps(report, indent=1, default=str))
    tmp.replace(out)
    print(summary(report))
    print(f"REPORT {out}")


if __name__ == "__main__":
    main()

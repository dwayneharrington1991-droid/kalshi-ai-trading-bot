"""Shadow-only combined opening decision engine for 8768. No activation path.

Combines the running AI's immutable opening probability with the experimental
models from ``shadow_upgrade`` and produces one opening decision per contract
(UP, DOWN or WAIT, a calibrated probability and a short reason). Nothing here
is imported by the live bot, and no journal, artifact, registry or setting is
written.

Data segments (fixed by the previous shadow run, so nothing is re-selected on
data that has already been looked at):

* development: contracts closing before the previous run's holdout started.
  Every choice (combiner, operating threshold, frontier thresholds) is made
  here, from chronological out-of-fold predictions only.
* previous holdout: already viewed in aggregate; reported descriptively and
  never used for any choice.
* fresh: contracts closing after the previous run's as-of time. No one has
  seen them. The frozen procedure scores them once; reruns add newer
  contracts with the SAME frozen training data and choices.

Base probabilities (all available at the opening):
  existing   the running AI's recorded opening probability (immutable record)
  logistic   wide regularised logistic over frozen opening features
  logistic_aug / gbdt_aug  the same families trained also on RETROSPECTIVE
             candle-only rows (candles closed AND received by each opening,
             official outcomes). Retrospective rows are training data only;
             they are never scored.

Pre-registered combiners (selected by development prequential log loss):
  existing_only, logistic_only, mean_existing_logistic, mean_existing_aug,
  stack (L2 logistic on base logits + agreement + spread, trained only on
  earlier out-of-fold rows), stack_conditional (stack + high-volatility
  interactions; the volatility cut comes from earlier rows only).
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from . import learning, opening_models, opening_research as research, shadow_upgrade as su
from .calibration import fit_platt
from .model import timestamp
from .scores import score, wilson_interval

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

REPORT_FORMAT = "kxbtc-combined-opening-8768-report-v1"
MIN_TRAIN = 60
BLOCK = 10
MIN_STACK_ROWS = 40
STACK_L2 = 10.0
MIN_OPERATING_CALLS = 20
BASES = ("existing", "logistic", "logistic_aug", "gbdt_aug")
COMBINERS = ("existing_only", "logistic_only", "mean_existing_logistic", "mean_existing_aug", "stack",
             "stack_conditional")
EPS = 1e-6


def _clip(p):
    return min(1 - EPS, max(EPS, float(p)))


def _logit(p):
    p = _clip(p)
    return math.log(p / (1 - p))


def _existing(row):
    p = row.get("recorded_up")
    return _clip(p) if type(p) in (int, float) and math.isfinite(p) and 0 <= p <= 1 else None


# ------------------------------------------------------------------ bases


def _fit_bases(live_train, live_validation, retro_train):
    """{base: predict(row)}; fitted on training rows, Platt-calibrated on the
    (live) validation rows only. Retrospective rows only ever enter training."""
    out = {}
    for name, rows, fitter in (("logistic", live_train, su._fit_logistic),
                               ("logistic_aug", retro_train + live_train, su._fit_logistic),
                               ("gbdt_aug", retro_train + live_train, su._fit_gbdt)):
        rows = sorted(rows, key=lambda r: (r["closes_at"], r["ticker"]))
        if len(rows) < 50 or min(sum(r["outcome"] == y for r in rows) for y in (0, 1)) < 10:
            continue
        try:
            raw = fitter(rows)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            continue
        out[name] = su._calibrated(raw, live_validation)
    return out


def _retro_before(retro, start):
    return [r for r in retro if timestamp(r["label_available_at"]) <= start and timestamp(r["closes_at"]) <= start]


def out_of_fold(live, retro, progress=None):
    """Chronological expanding folds over live rows. Each predicted contract's
    label was unavailable to every model (and every calibration) that scored it."""
    oof = {}
    ends = list(range(MIN_TRAIN, len(live), BLOCK))
    for end in ends:
        past, test = live[:end], live[end:end + BLOCK]
        split = int(len(past) * 0.8)
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 40 or len(validation) < 10 or not test:
            continue
        start = min(timestamp(r["timestamp"]) for r in test)
        t0 = time.monotonic()
        bases = _fit_bases(train, validation, _retro_before(retro, start))
        for r in test:
            oof[r["ticker"]] = {name: predict(r) for name, predict in bases.items()}
            oof[r["ticker"]]["existing"] = _existing(r)
        if progress:
            progress(f"fold up to {end}: {len(test)} tested, bases {sorted(bases)} in {time.monotonic() - t0:.1f}s")
    return oof


# ------------------------------------------------------------- combiners


def _stack_x(b, conditional=False, high_vol=0.0):
    e = _logit(b["existing"]) if b.get("existing") is not None else 0.0
    lg = _logit(b["logistic"]) if b.get("logistic") is not None else 0.0
    ga = _logit(b["gbdt_aug"]) if b.get("gbdt_aug") is not None else 0.0
    agree = 1.0 if (b.get("existing") is not None and b.get("logistic") is not None
                    and (b["existing"] >= 0.5) == (b["logistic"] >= 0.5)) else 0.0
    x = [1.0, e, lg, ga, agree, abs(e - lg), 1.0 if b.get("existing") is None else 0.0]
    if conditional:
        x += [high_vol, high_vol * e, high_vol * lg]
    return x


def fit_l2_logistic(X, y, l2=STACK_L2, iters=50):
    """Damped Newton L2 logistic (intercept unpenalised)."""
    X, y = np.asarray(X, float), np.asarray(y, float)
    w = np.zeros(X.shape[1])
    pen = np.full(X.shape[1], l2); pen[0] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        g = X.T @ (p - y) + pen * w
        H = (X * (p * (1 - p))[:, None]).T @ X + np.diag(pen + 1e-9)
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def _apply(w, x):
    return _clip(1 / (1 + math.exp(-max(-30, min(30, sum(a * b for a, b in zip(w, x)))))))


def _simple(name, b):
    e, lg, la = b.get("existing"), b.get("logistic"), b.get("logistic_aug")
    if name == "existing_only":
        return e
    if name == "logistic_only":
        return lg
    pair = (e, lg) if name == "mean_existing_logistic" else (e, la)
    vals = [v for v in pair if v is not None]
    return learning.sigmoid(sum(_logit(v) for v in vals) / len(vals)) if vals else None


def prequential(live, oof):
    """{combiner: {ticker: p}}: each stacked prediction comes from a stacker
    fitted only on EARLIER out-of-fold rows whose labels were available before
    the predicted contract opened. Until enough earlier rows exist the stack
    falls back to the equal-weight mean (recorded as such)."""
    rows = [r for r in live if r["ticker"] in oof]
    out = {name: {} for name in COMBINERS}
    for i, r in enumerate(rows):
        b = oof[r["ticker"]]
        for name in COMBINERS[:4]:
            p = _simple(name, b)
            if p is not None:
                out[name][r["ticker"]] = _clip(p)
        start = timestamp(r["timestamp"])
        past = [q for q in rows[:i] if timestamp(q["label_available_at"]) <= start]
        fallback = _simple("mean_existing_logistic", b)
        if np is None or len(past) < MIN_STACK_ROWS:
            if fallback is not None:
                out["stack"][r["ticker"]] = out["stack_conditional"][r["ticker"]] = _clip(fallback)
            continue
        y = [q["outcome"] for q in past]
        w = fit_l2_logistic([_stack_x(oof[q["ticker"]]) for q in past], y)
        out["stack"][r["ticker"]] = _apply(w, _stack_x(b))
        vols = sorted(v for q in past if type(v := q["features"].get("volatility_24h")) in (int, float))
        cut = vols[2 * len(vols) // 3] if len(vols) >= 15 else None
        hv = lambda q: 1.0 if cut is not None and type(q["features"].get("volatility_24h")) in (int, float) \
            and q["features"]["volatility_24h"] > cut else 0.0
        wc = fit_l2_logistic([_stack_x(oof[q["ticker"]], True, hv(q)) for q in past], y)
        out["stack_conditional"][r["ticker"]] = _apply(wc, _stack_x(b, True, hv(r)))
    return out


# ------------------------------------------------------------ measurement


def _metrics(pairs, markets):
    n = len(pairs)
    correct = sum((p >= 0.5) == bool(r["outcome"]) for p, r in pairs)
    s = score([p for p, _ in pairs], [r["outcome"] for _, r in pairs]) if pairs else {}
    return {"calls": n, "correct": correct, "wrong": n - correct, "accuracy": correct / n if n else None,
            "accuracy_wilson_95": wilson_interval(correct, n) if n else None, "markets": markets,
            "coverage": n / markets if markets else 0.0, "brier": s.get("brier"), "log_loss": s.get("log_loss")}


def _called(preds, rows, t):
    return [(preds[r["ticker"]], r) for r in rows if r["ticker"] in preds and max(preds[r["ticker"]], 1 - preds[r["ticker"]]) >= t]


def choose_thresholds(preds, dev):
    """Development-only. Operating threshold: the lowest grid threshold whose
    calls are credibly better than chance (Wilson lower bound > 50%) with at
    least MIN_OPERATING_CALLS calls -- the most correct calls with a credible
    edge. Frontier: the lowest threshold reaching each accuracy target with at
    least shadow_upgrade.MIN_DEVELOPMENT_CALLS calls."""
    rows = [r for r in dev if r["ticker"] in preds]
    operating = None
    frontier = {}
    for t in su.THRESHOLD_GRID:
        m = _metrics(_called(preds, rows, t), len(rows))
        if operating is None and m["calls"] >= MIN_OPERATING_CALLS and m["accuracy_wilson_95"][0] > 0.5:
            operating = (t, m)
        for target in su.ACCURACY_TARGETS:
            key = f"{int(target * 100)}%"
            if key not in frontier and m["calls"] >= su.MIN_DEVELOPMENT_CALLS and m["accuracy"] >= target:
                frontier[key] = (t, m)
    return operating, frontier


def decision(p, threshold, bases):
    """One opening decision with a concise reason."""
    if p is None:
        return {"direction": "WAIT", "up": None, "reason": "no combined probability available at the opening"}
    direction = ("UP" if p >= 0.5 else "DOWN") if threshold is not None and max(p, 1 - p) >= threshold else "WAIT"
    e, lg = bases.get("existing"), bases.get("logistic")
    agree = (e is not None and lg is not None and (e >= 0.5) == (lg >= 0.5))
    parts = [f"combined {p:.2f}"]
    if e is not None:
        parts.append(f"existing {e:.2f}")
    for name in ("logistic", "logistic_aug", "gbdt_aug"):
        if bases.get(name) is not None:
            parts.append(f"{name} {bases[name]:.2f}")
    parts.append("models agree" if agree else "models disagree" if e is not None and lg is not None else "partial inputs")
    if direction == "WAIT":
        parts.append("below the development-validated threshold" if threshold is not None else "no credible edge in development")
    return {"direction": direction, "up": round(p, 4), "reason": "; ".join(parts)}


def agreement(oof, rows):
    out = {}
    for label, want in (("agree", True), ("disagree", False)):
        pairs = []
        for r in rows:
            b = oof.get(r["ticker"]) or {}
            e, lg = b.get("existing"), b.get("logistic")
            if e is None or lg is None or ((e >= 0.5) == (lg >= 0.5)) != want:
                continue
            pairs.append((learning.sigmoid((_logit(e) + _logit(lg)) / 2), r))
        out[label] = _metrics(pairs, len(pairs))
    return out


def calibration_bins(preds, rows):
    bins = {}
    for r in rows:
        if r["ticker"] not in preds:
            continue
        c = max(preds[r["ticker"]], 1 - preds[r["ticker"]])
        key = "50-55" if c < 0.55 else "55-60" if c < 0.60 else "60-70" if c < 0.70 else "70+"
        bins.setdefault(key, []).append((preds[r["ticker"]], r))
    return {k: {"n": len(v), "mean_confidence": round(sum(max(p, 1 - p) for p, _ in v) / len(v), 4),
                "accuracy": round(sum((p >= 0.5) == bool(r["outcome"]) for p, r in v) / len(v), 4)}
            for k, v in sorted(bins.items())}


def family_value(dev, progress=None):
    """Exploratory: does each indicator family add out-of-sample value? The
    wide logistic is refitted in the same chronological folds without that
    family; positive delta = log loss is WORSE without it (the family helps).
    Descriptive on a small sample; no family is selected or dropped here."""
    configs = {"all": su.ALL_FEATURES}
    for family, feats in research.FEATURE_GROUPS.items():
        configs["without_" + family] = tuple(f for f in su.ALL_FEATURES if f not in feats)
    preds = {name: {} for name in configs}
    for end in range(MIN_TRAIN, len(dev), BLOCK):
        past, test = dev[:end], dev[end:end + BLOCK]
        split = int(len(past) * 0.8)
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 40 or len(validation) < 10 or not test:
            continue
        for name, feats in configs.items():
            try:
                predict = su._calibrated(su._fit_logistic(train, feats), validation)
            except (ValueError, ZeroDivisionError, FloatingPointError):
                continue
            preds[name].update({r["ticker"]: predict(r) for r in test})
    common = [r for r in dev if all(r["ticker"] in p for p in preds.values())]
    if len(common) < su.MIN_DEVELOPMENT_CALLS:
        return {"status": "INSUFFICIENT_DATA", "contracts": len(common)}
    def loss(name):
        return score([preds[name][r["ticker"]] for r in common], [r["outcome"] for r in common])["log_loss"]
    def acc(name):
        return sum((preds[name][r["ticker"]] >= 0.5) == bool(r["outcome"]) for r in common) / len(common)
    full = loss("all")
    out = {"contracts": len(common), "all_features": {"log_loss": full, "accuracy": acc("all")}, "families": {}}
    for name in configs:
        if name != "all":
            out["families"][name[len("without_"):]] = {
                "log_loss_without": loss(name), "delta_log_loss_if_removed": loss(name) - full,
                "accuracy_without": acc(name), "verdict": "helps" if loss(name) - full > 0.002
                else "hurts" if loss(name) - full < -0.002 else "no measurable effect"}
    return out


def _net_ev(pairs):
    m = research.call_metrics(pairs) if pairs else {}
    return {k: m.get(k) for k in ("calls_with_executable_quote", "mean_net_pnl_per_contract", "net_pnl_95",
                                  "total_net_pnl_one_contract_each")}


def diagnostics(segments, retro_info):
    out = {"retrospective_training_rows": retro_info}
    for name, rows in segments.items():
        n = len(rows)
        fam = {}
        for family, feats in research.FEATURE_GROUPS.items():
            present = sum(any(r["features"].get(f) is not None for f in feats) for r in rows)
            fam[family] = round(present / n, 3) if n else None
        models = {}
        for r in rows:
            models[r.get("recorded_model")] = models.get(r.get("recorded_model"), 0) + 1
        out[name] = {"contracts": n, "up_rate": round(sum(r["outcome"] for r in rows) / n, 4) if n else None,
                     "existing_probability_available": sum(_existing(r) is not None for r in rows),
                     "feature_family_presence": fam, "recorded_models": models,
                     "first_close": rows[0]["closes_at"] if rows else None,
                     "last_close": rows[-1]["closes_at"] if rows else None}
    return out


# ---------------------------------------------------------------- driver


def evaluate(live, retro, holdout_start, fresh_after, progress=None, retro_info=None):
    research.add_interactions(live)
    research.add_interactions(retro)
    live = opening_models._validate_rows(live)
    retro = sorted(retro, key=lambda r: (r["closes_at"], r["ticker"]))
    hs, fa = timestamp(holdout_start), timestamp(fresh_after)
    dev = [r for r in live if timestamp(r["closes_at"]) < hs]
    prior = [r for r in live if hs <= timestamp(r["closes_at"]) <= fa]
    fresh = [r for r in live if timestamp(r["closes_at"]) > fa]
    report = {"format": REPORT_FORMAT, "shadow_only": True, "activation_path": None,
              "boundaries": {"previous_holdout_start": holdout_start, "fresh_after": fresh_after},
              "preregistered": {"bases": BASES, "combiners": COMBINERS, "min_train": MIN_TRAIN, "block": BLOCK,
                                "min_stack_rows": MIN_STACK_ROWS, "stack_l2": STACK_L2,
                                "operating_rule": "lowest threshold with >= 20 development calls and Wilson lower bound > 50%",
                                "selection": "lowest development prequential log loss"},
              "diagnostics": diagnostics({"development": dev, "previous_holdout": prior, "fresh": fresh}, retro_info)}
    pre_fresh = dev + prior
    oof = out_of_fold(pre_fresh, retro, progress)
    preds = prequential(pre_fresh, oof)
    dev_scored = [r for r in dev if r["ticker"] in oof]
    report["development_out_of_fold_contracts"] = len(dev_scored)
    if len(dev_scored) < su.MIN_DEVELOPMENT_CALLS:
        report["status"] = "INSUFFICIENT_DEVELOPMENT_DATA"
        return report
    losses = {}
    for name, p in preds.items():
        pairs = [(p[r["ticker"]], r) for r in dev_scored if r["ticker"] in p]
        if len(pairs) == len(dev_scored):
            losses[name] = score([q for q, _ in pairs], [r["outcome"] for _, r in pairs])["log_loss"]
    selected = min(losses, key=losses.get)
    operating, frontier = choose_thresholds(preds[selected], dev_scored)
    report["development_log_loss"] = losses
    report["selected_combiner"] = selected
    report["development"] = {
        name: {"every_market": _metrics([(p[r["ticker"]], r) for r in dev_scored if r["ticker"] in p], len(dev_scored)),
               "calibration": calibration_bins(p, dev_scored)} for name, p in preds.items()}
    report["agreement_development"] = agreement(oof, dev_scored)
    report["family_value_development"] = family_value(dev, progress)
    report["operating_threshold"] = operating[0] if operating else None
    report["operating_development"] = operating[1] if operating else None
    report["frontier_development"] = {k: {"threshold": t, **{x: m[x] for x in ("calls", "accuracy", "coverage")}}
                                      for k, (t, m) in frontier.items()}
    prior_scored = [r for r in prior if r["ticker"] in preds[selected]]
    report["previous_holdout_descriptive"] = {
        "note": "already viewed in aggregate; descriptive only, never used for any choice",
        "selected_every_market": _metrics([(preds[selected][r["ticker"]], r) for r in prior_scored], len(prior)),
        "existing_displayed": _metrics(su.current_ai(prior)[1], len(prior)),
    }
    # Frozen final procedure for FRESH contracts: bases fitted on every
    # pre-fresh live row (+ retrospective rows labelled before the boundary);
    # the stacker on every pre-fresh out-of-fold row; thresholds from development.
    if fresh:
        split = int(len(pre_fresh) * 0.8)
        train, validation, _ = opening_models._purge(pre_fresh[:split], pre_fresh[split:], fresh[:1])
        bases = _fit_bases(train, validation, _retro_before(retro, fa))
        fresh_bases = {r["ticker"]: {**{n: f(r) for n, f in bases.items()}, "existing": _existing(r)} for r in fresh}
        past = [r for r in pre_fresh if r["ticker"] in oof]
        final = {}
        if selected.startswith("stack") and np is not None and len(past) >= MIN_STACK_ROWS:
            cond = selected == "stack_conditional"
            vols = sorted(v for q in past if type(v := q["features"].get("volatility_24h")) in (int, float))
            cut = vols[2 * len(vols) // 3] if len(vols) >= 15 else None
            hv = lambda q: 1.0 if cut is not None and type(q["features"].get("volatility_24h")) in (int, float) \
                and q["features"]["volatility_24h"] > cut else 0.0
            w = fit_l2_logistic([_stack_x(oof[q["ticker"]], cond, hv(q)) for q in past], [q["outcome"] for q in past])
            for r in fresh:
                final[r["ticker"]] = _apply(w, _stack_x(fresh_bases[r["ticker"]], cond, hv(r)))
        else:
            name = selected if not selected.startswith("stack") else "mean_existing_logistic"
            for r in fresh:
                p = _simple(name, fresh_bases[r["ticker"]])
                if p is not None:
                    final[r["ticker"]] = _clip(p)
        decisions = {r["ticker"]: decision(final.get(r["ticker"]), report["operating_threshold"], fresh_bases[r["ticker"]])
                     for r in fresh}
        called = [(final[t], r) for r in fresh if (t := r["ticker"]) in final and decisions[t]["direction"] != "WAIT"]
        logistic_fresh = {t: b["logistic"] for t, b in fresh_bases.items() if b.get("logistic") is not None}
        report["fresh"] = {
            "contracts": len(fresh),
            "combined_operating": _metrics(called, len(fresh)),
            "combined_every_market": _metrics([(final[r["ticker"]], r) for r in fresh if r["ticker"] in final], len(fresh)),
            "logistic_every_market": _metrics([(logistic_fresh[r["ticker"]], r) for r in fresh if r["ticker"] in logistic_fresh], len(fresh)),
            "logistic_at_operating_threshold": _metrics(_called(logistic_fresh, fresh, report["operating_threshold"] or 1.01), len(fresh)),
            "existing_displayed": _metrics(su.current_ai(fresh)[1], len(fresh)),
            "existing_every_market": _metrics([(e, r) for r in fresh if (e := _existing(r)) is not None], len(fresh)),
            "frontier": {k: _metrics(_called(final, fresh, t), len(fresh)) for k, (t, _m) in frontier.items()},
            "net_ev_after_spread_fees": {"combined_operating": _net_ev(called),
                                         "existing_displayed": _net_ev(su.current_ai(fresh)[1])},
            "recent_decisions": [{"ticker": r["ticker"], **decisions[r["ticker"]], "outcome_up": r["outcome"]}
                                 for r in fresh[-10:]],
        }
    else:
        report["fresh"] = {"contracts": 0, "status": "NO_FRESH_SETTLED_CONTRACTS_YET"}
    report["status"] = "MEASURED"
    return report


def summary(report):
    pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"
    ci = lambda m: "n/a" if not m.get("accuracy_wilson_95") else "-".join(pct(v) for v in m["accuracy_wilson_95"])
    line = lambda label, m: (f"{label}: {m['correct']}/{m['calls']} = {pct(m['accuracy'])} [{ci(m)}], "
                             f"coverage {pct(m['coverage'])} of {m['markets']}")
    d = report["diagnostics"]
    out = [f"COMBINED OPENING 8768 ({report['format']}) status {report.get('status')}",
           "segments: " + ", ".join(f"{k} {d[k]['contracts']} (UP rate {d[k]['up_rate']})"
                                    for k in ("development", "previous_holdout", "fresh")),
           f"retrospective training rows: {(d.get('retrospective_training_rows') or {}).get('reconstructed')}",
           f"development out-of-fold contracts: {report.get('development_out_of_fold_contracts')}"]
    if report.get("status") != "MEASURED":
        return "\n".join(out)
    out.append(f"selected combiner: {report['selected_combiner']} (dev log loss "
               + ", ".join(f"{k} {v:.4f}" for k, v in sorted(report["development_log_loss"].items(), key=lambda kv: kv[1])) + ")")
    for name, e in report["development"].items():
        out.append(line(f"  dev {name} every market", e["every_market"]))
    a = report["agreement_development"]
    out.append(line("  dev existing+logistic AGREE", a["agree"]) + " | " + line("DISAGREE", a["disagree"]))
    fv = report.get("family_value_development") or {}
    if fv.get("families"):
        out.append(f"indicator families (dev, {fv['contracts']} contracts, exploratory): " + "; ".join(
            f"{k} {v['verdict']} ({v['delta_log_loss_if_removed']:+.4f})" for k, v in fv["families"].items()))
    out.append(f"operating threshold (dev): {report['operating_threshold']}; frontier (dev): {report['frontier_development']}")
    f = report["fresh"]
    if f.get("contracts"):
        out.append(f"FRESH UNSEEN CONTRACTS: {f['contracts']}")
        for key in ("combined_operating", "combined_every_market", "logistic_at_operating_threshold",
                    "logistic_every_market", "existing_displayed", "existing_every_market"):
            out.append(line("  " + key, f[key]))
        for k, m in f["frontier"].items():
            out.append(line(f"  combined at {k} dev threshold", m))
        for k, v in f["net_ev_after_spread_fees"].items():
            out.append(f"  net EV after spread/fees/slippage, {k}: {v['mean_net_pnl_per_contract']} per contract "
                       f"over {v['calls_with_executable_quote']} priced calls (95% {v['net_pnl_95']})")
        for dcs in f["recent_decisions"][-5:]:
            out.append(f"  {dcs['ticker']}: {dcs['direction']} ({dcs['reason']}) -> outcome {'UP' if dcs['outcome_up'] else 'DOWN'}")
    else:
        out.append("FRESH: no settled contracts after the boundary yet; rerun later")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-report", type=Path, required=True,
                        help="shadow_upgrade report whose holdout start and as-of fix the segments")
    parser.add_argument("--asof", help="ISO-8601 cutoff; only labels observed by then are used")
    args = parser.parse_args(argv)
    out = args.output.resolve()
    if out.suffix != ".json" or out == args.journal.resolve() or out.name.startswith(args.journal.name):
        parser.error("output must be a separate .json report")
    previous = json.loads(args.previous_report.read_text())
    holdout_start, fresh_after = previous["holdout_starts"], previous["asof"]
    asof = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    with closing(sqlite3.connect(f"file:{args.journal.resolve()}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        live = research.load(db, asof)
        retro, info = research.retrospective_rows(db, {r["ticker"] for r in live}, asof=asof)
    report = evaluate(live, retro, holdout_start, fresh_after, progress=lambda m: print("SHADOW:", m, flush=True),
                      retro_info=info)
    report["asof"] = asof.isoformat()
    tmp = out.with_suffix(".json.next")
    tmp.write_text(json.dumps(report, indent=1, default=str))
    tmp.replace(out)
    print(summary(report))
    print(f"REPORT {out}")


if __name__ == "__main__":
    main()

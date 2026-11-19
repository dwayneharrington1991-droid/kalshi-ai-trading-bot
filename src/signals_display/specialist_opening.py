"""Shadow-only five-specialist opening architecture for 8768. No activation path.

Specialists (every frozen as-of-opening input belongs to exactly ONE of them,
so no indicator is counted twice):

1. trend      momentum, acceleration, breakout, EMA distances/gaps, RSI, MACD,
              VWAP deviation, Bollinger position, volume acceleration
2. flow       order book and trade flow (CVD, imbalances, microprice, effective
              spread, trade intensity), perpetual basis, Kalshi pre-open price
3. pattern    candle shape and relative volume, trained also on RETROSPECTIVE
              candle-only rows (historical patterns); training only, never scored
4. regime     volatility level/change, ATR expansion, rising/falling regime and
              their interactions with momentum and trend
5. confidence not a direction model: a reliability gate that learns, from
              earlier rows only, when the master's call tends to be right
              (model agreement, dispersion, conviction, volatility regime) and
              turns low-reliability calls into WAIT

Each directional specialist is a regularised logistic with the statistical
opening baseline as its offset. The master engine therefore stacks each
specialist's INCREMENT over the baseline (logit p_k - logit baseline), plus one
baseline term and the running AI's recorded probability, so the shared
baseline is not counted five times. Stack weights are L2-regularised and
fitted only on earlier out-of-fold rows (prequential).

Pre-registered candidates, all scored on identical contracts:
  existing_only, logistic_only (wide logistic), combined_mean_existing_logistic,
  combined_mean_existing_aug (the combined AI's simple blends),
  master_full, master_without_<each of existing/trend/flow/pattern/regime>,
  master_existing_pattern (two inputs), master_specialists_only (no existing AI)
The selected candidate is the lowest development prequential log loss. A
specialist "adds value" only if removing it makes development log loss worse.

Segments come from the first shadow run exactly as in combined_opening:
development (all choices), previous holdout (descriptive only), fresh unseen
contracts (scored once by the frozen procedure).
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

from . import combined_opening as co, learning, opening_models, opening_research as research, shadow_upgrade as su
from .model import timestamp
from .scores import score

REPORT_FORMAT = "kxbtc-specialist-opening-8768-report-v1"

SPECIALIST_FEATURES = {
    "trend": ("return_1m", "return_5m", "return_15m", "return_60m", "return_24h", "acceleration", "breakout_distance",
              "body_fraction", "rsi_14", "macd_scaled", "ema_gap", "vwap_deviation", "bollinger_z",
              "opening_ema9_distance", "opening_ema9_21_gap", "opening_ema50_distance", "opening_ema200_distance",
              "opening_volume_acceleration"),
    "flow": ("spot_cvd_delta_fraction", "perpetual_cvd_delta_fraction", "spot_book_dominance",
             "spot_book_imbalance_top3", "spot_micro_price_deviation", "advanced_flow_imbalance", "advanced_cvd_slope",
             "advanced_book_imbalance", "advanced_effective_spread", "advanced_trade_intensity",
             "advanced_vwap_deviation_fraction", "spot_depth_imbalance", "derivatives_basis",
             "kalshi_preopen_mid_logit"),
    "pattern": ("upper_wick", "lower_wick", "range_fraction", "candle_body_range_ratio", "candle_close_range_position",
                "candle_volume_weighted_move", "relative_volume"),
    "regime": ("volatility_24h", "volatility_change", "candle_atr_zscore_20", "candle_unusual_expansion",
               "regime_rising", "regime_falling", "ix_return_15m_x_rising", "ix_return_15m_x_falling",
               "ix_ema9_21_gap_x_rising", "ix_ema9_21_gap_x_falling"),
}
SPECIALISTS = tuple(SPECIALIST_FEATURES)
USES_RETROSPECTIVE = {"trend": True, "flow": False, "pattern": True, "regime": True}
INPUTS = ("existing",) + SPECIALISTS
MASTERS = {
    "master_full": INPUTS,
    **{f"master_without_{name}": tuple(i for i in INPUTS if i != name) for name in INPUTS},
    "master_existing_pattern": ("existing", "pattern"),
    "master_specialists_only": SPECIALISTS,
}
REFERENCES = ("existing_only", "logistic_only", "combined_mean_existing_logistic", "combined_mean_existing_aug")
CANDIDATES = REFERENCES + tuple(MASTERS)

assert len({f for feats in SPECIALIST_FEATURES.values() for f in feats}) == sum(
    len(f) for f in SPECIALIST_FEATURES.values()), "a feature belongs to more than one specialist"


# ------------------------------------------------------------- base layer


def _fit_layer(train, validation, retro_train):
    """Raw specialist predictors (offset logistic, fitted on training rows) and
    the calibrated wide-logistic references."""
    out = {}
    for name, feats in SPECIALIST_FEATURES.items():
        rows = sorted((retro_train if USES_RETROSPECTIVE[name] else []) + train,
                      key=lambda r: (r["closes_at"], r["ticker"]))
        if len(rows) < 50 or min(sum(r["outcome"] == y for r in rows) for y in (0, 1)) < 10:
            continue
        try:
            out[name] = su._fit_logistic(rows, feats)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            continue
    for name, rows in (("logistic", train), ("logistic_aug", retro_train + train)):
        rows = sorted(rows, key=lambda r: (r["closes_at"], r["ticker"]))
        try:
            out[name] = su._calibrated(su._fit_logistic(rows), validation)
        except (ValueError, ZeroDivisionError, FloatingPointError):
            continue
    return out


def _layer_values(layer, row):
    values = {name: f(row) for name, f in layer.items()}
    values["existing"] = co._existing(row)
    values["baseline"] = co._clip(row["baseline"])
    return values


def out_of_fold(live, retro, progress=None):
    oof = {}
    for end in range(co.MIN_TRAIN, len(live), co.BLOCK):
        past, test = live[:end], live[end:end + co.BLOCK]
        split = int(len(past) * 0.8)
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 40 or len(validation) < 10 or not test:
            continue
        start = min(timestamp(r["timestamp"]) for r in test)
        t0 = time.monotonic()
        layer = _fit_layer(train, validation, co._retro_before(retro, start))
        for r in test:
            oof[r["ticker"]] = _layer_values(layer, r)
        if progress:
            progress(f"specialist fold up to {end}: {len(test)} tested, {sorted(layer)} in {time.monotonic() - t0:.1f}s")
    return oof


# ------------------------------------------------------------ master layer


def _increment(v, name):
    p = v.get(name)
    return co._logit(p) - co._logit(v["baseline"]) if p is not None else 0.0


def master_x(v, inputs):
    x = [1.0, co._logit(v["baseline"])]
    for name in inputs:
        if name == "existing":
            e = v.get("existing")
            x += [co._logit(e) if e is not None else 0.0, 1.0 if e is None else 0.0]
        else:
            x.append(_increment(v, name))
    return x


def _fallback(v, inputs):
    """Before enough earlier rows exist: baseline plus the mean specialist
    increment, averaged with the existing AI when present (recorded as such)."""
    incs = [_increment(v, n) for n in inputs if n != "existing" and v.get(n) is not None]
    z = co._logit(v["baseline"]) + (sum(incs) / len(incs) if incs else 0.0)
    if "existing" in inputs and v.get("existing") is not None:
        z = (z + co._logit(v["existing"])) / 2
    return co._clip(learning.sigmoid(z))


def _reference(name, v):
    e = v.get("existing")
    if name == "existing_only":
        return e
    if name == "logistic_only":
        return v.get("logistic")
    other = v.get("logistic") if name == "combined_mean_existing_logistic" else v.get("logistic_aug")
    vals = [p for p in (e, other) if p is not None]
    return co._clip(learning.sigmoid(sum(co._logit(p) for p in vals) / len(vals))) if vals else None


def gate_x(v, p, high_vol):
    direction_up = p >= 0.5
    votes = [v[n] >= v["baseline"] if n != "existing" else v[n] >= 0.5
             for n in INPUTS if v.get(n) is not None]
    agree = sum(vote == direction_up for vote in votes) / len(votes) if votes else 0.5
    incs = [_increment(v, n) for n in SPECIALISTS if v.get(n) is not None]
    mean = sum(incs) / len(incs) if incs else 0.0
    dispersion = math.sqrt(sum((i - mean) ** 2 for i in incs) / len(incs)) if incs else 0.0
    return [1.0, abs(co._logit(p)), agree, min(dispersion, 5.0), high_vol]


def _high_vol_rule(past):
    vols = sorted(x for q in past if type(x := q["features"].get("volatility_24h")) in (int, float))
    cut = vols[2 * len(vols) // 3] if len(vols) >= 15 else None
    return lambda q: 1.0 if cut is not None and type(q["features"].get("volatility_24h")) in (int, float) \
        and q["features"]["volatility_24h"] > cut else 0.0


def prequential(rows, oof):
    """{candidate: {ticker: p}} plus {ticker: P(correct)} from the confidence
    gate of every master, all fitted on strictly earlier rows."""
    rows = [r for r in rows if r["ticker"] in oof]
    preds = {name: {} for name in CANDIDATES}
    reliability = {name: {} for name in MASTERS}
    for i, r in enumerate(rows):
        v = oof[r["ticker"]]
        for name in REFERENCES:
            p = _reference(name, v)
            if p is not None:
                preds[name][r["ticker"]] = co._clip(p)
        start = timestamp(r["timestamp"])
        past = [q for q in rows[:i] if timestamp(q["label_available_at"]) <= start]
        enough = co.np is not None and len(past) >= co.MIN_STACK_ROWS
        y = [q["outcome"] for q in past]
        hv = _high_vol_rule(past)
        for name, inputs in MASTERS.items():
            if not enough:
                preds[name][r["ticker"]] = _fallback(v, inputs)
                continue
            w = co.fit_l2_logistic([master_x(oof[q["ticker"]], inputs) for q in past], y)
            preds[name][r["ticker"]] = co._apply(w, master_x(v, inputs))
            gated = [q for q in past if q["ticker"] in preds[name]]
            if len(gated) >= co.MIN_STACK_ROWS:
                gy = [int((preds[name][q["ticker"]] >= 0.5) == bool(q["outcome"])) for q in gated]
                if 0 < sum(gy) < len(gy):
                    gw = co.fit_l2_logistic([gate_x(oof[q["ticker"]], preds[name][q["ticker"]], hv(q)) for q in gated], gy)
                    reliability[name][r["ticker"]] = co._apply(gw, gate_x(v, preds[name][r["ticker"]], hv(r)))
    return preds, reliability


def choose_gate(preds, reliability, dev):
    """Development-only: the lowest reliability cut whose gated calls are
    credibly better than chance with >= MIN_OPERATING_CALLS calls."""
    rows = [r for r in dev if r["ticker"] in reliability and r["ticker"] in preds]
    for g in su.THRESHOLD_GRID:
        pairs = [(preds[r["ticker"]], r) for r in rows if reliability[r["ticker"]] >= g]
        m = co._metrics(pairs, len(rows))
        if m["calls"] >= co.MIN_OPERATING_CALLS and m["accuracy_wilson_95"][0] > 0.5:
            return g, m
    return None, None


# ---------------------------------------------------------------- driver


def _reason(p, v, direction, rule):
    votes = []
    for name in INPUTS:
        if v.get(name) is None:
            continue
        up = v[name] >= 0.5 if name == "existing" else v[name] >= v["baseline"]
        votes.append(f"{name} {'UP' if up else 'DOWN'}")
    text = f"master {p:.2f} ({', '.join(votes)})"
    return text + ("" if direction != "WAIT" else f"; WAIT: {rule}")


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
              "preregistered": {"specialist_features": SPECIALIST_FEATURES, "masters": MASTERS,
                                "references": REFERENCES, "selection": "lowest development prequential log loss",
                                "specialist_value": "removing it worsens development log loss by > 0.002"},
              "diagnostics": co.diagnostics({"development": dev, "previous_holdout": prior, "fresh": fresh}, retro_info)}
    pre_fresh = dev + prior
    oof = out_of_fold(pre_fresh, retro, progress)
    preds, reliability = prequential(pre_fresh, oof)
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
    report["selected"] = selected
    report["development_log_loss"] = dict(sorted(losses.items(), key=lambda kv: kv[1]))
    report["development"] = {name: co._metrics([(p[r["ticker"]], r) for r in dev_scored if r["ticker"] in p],
                                               len(dev_scored)) for name, p in preds.items()}
    full = losses.get("master_full")
    report["specialist_value"] = {
        name: {"delta_log_loss_if_removed": losses[f"master_without_{name}"] - full,
               "verdict": "adds value" if losses[f"master_without_{name}"] - full > 0.002
               else "no measurable value (recommend leaving out)"}
        for name in INPUTS if full is not None and f"master_without_{name}" in losses}
    operating, frontier = co.choose_thresholds(preds[selected], dev_scored)
    report["operating_threshold"] = operating[0] if operating else None
    report["frontier_development"] = {k: {"threshold": t, **{x: m[x] for x in ("calls", "accuracy", "coverage")}}
                                      for k, (t, m) in frontier.items()}
    gate, gate_dev = (choose_gate(preds[selected], reliability[selected], dev_scored)
                      if selected in MASTERS else (None, None))
    use_gate = bool(gate_dev and operating and gate_dev["correct"] > operating[1]["correct"]
                    and gate_dev["accuracy"] >= operating[1]["accuracy"])
    report["confidence_gate"] = {"cut": gate, "development": gate_dev, "used": use_gate,
                                 "threshold_rule_development": operating[1] if operating else None,
                                 "rule": "used only if it gives more correct development calls at no lower accuracy"}
    report["previous_holdout_descriptive"] = {
        "note": "already viewed in aggregate; descriptive only, never used for any choice",
        "selected_every_market": co._metrics([(preds[selected][r["ticker"]], r) for r in prior
                                              if r["ticker"] in preds[selected]], len(prior))}
    if not fresh:
        report["fresh"] = {"contracts": 0, "status": "NO_FRESH_SETTLED_CONTRACTS_YET"}
        report["status"] = "MEASURED"
        return report
    # Frozen final procedure for fresh contracts.
    split = int(len(pre_fresh) * 0.8)
    train, validation, _ = opening_models._purge(pre_fresh[:split], pre_fresh[split:], fresh[:1])
    layer = _fit_layer(train, validation, co._retro_before(retro, fa))
    past = [r for r in pre_fresh if r["ticker"] in oof]
    hv = _high_vol_rule(past)
    weights = {name: co.fit_l2_logistic([master_x(oof[q["ticker"]], inputs) for q in past],
                                        [q["outcome"] for q in past])
               for name, inputs in MASTERS.items()} if co.np is not None and len(past) >= co.MIN_STACK_ROWS else {}
    gate_w = None
    if use_gate:
        gated = [q for q in past if q["ticker"] in preds[selected]]
        gate_w = co.fit_l2_logistic([gate_x(oof[q["ticker"]], preds[selected][q["ticker"]], hv(q)) for q in gated],
                                    [int((preds[selected][q["ticker"]] >= 0.5) == bool(q["outcome"])) for q in gated])
    final = {name: {} for name in CANDIDATES}
    decisions, latencies = {}, []
    for r in fresh:
        t0 = time.perf_counter()
        v = _layer_values(layer, r)
        for name in REFERENCES:
            p = _reference(name, v)
            if p is not None:
                final[name][r["ticker"]] = co._clip(p)
        for name, inputs in MASTERS.items():
            final[name][r["ticker"]] = co._apply(weights[name], master_x(v, inputs)) if name in weights \
                else _fallback(v, inputs)
        p = final[selected].get(r["ticker"])
        if p is None:
            d = {"direction": "WAIT", "up": None, "reason": "no probability available at the opening"}
        elif use_gate:
            ok = co._apply(gate_w, gate_x(v, p, hv(r))) >= gate
            d = {"direction": ("UP" if p >= 0.5 else "DOWN") if ok else "WAIT", "up": round(p, 4)}
            d["reason"] = _reason(p, v, d["direction"], "confidence gate: low expected reliability")
        else:
            ok = report["operating_threshold"] is not None and max(p, 1 - p) >= report["operating_threshold"]
            d = {"direction": ("UP" if p >= 0.5 else "DOWN") if ok else "WAIT", "up": round(p, 4)}
            d["reason"] = _reason(p, v, d["direction"], "below the development-validated threshold"
                                  if report["operating_threshold"] is not None else "no credible edge in development")
        latencies.append((time.perf_counter() - t0) * 1000)
        decisions[r["ticker"]] = d
    called = [(final[selected][r["ticker"]], r) for r in fresh if decisions[r["ticker"]]["direction"] != "WAIT"]
    thr = report["operating_threshold"] or 1.01
    latencies.sort()
    report["fresh"] = {
        "contracts": len(fresh),
        "selected_decisions": co._metrics(called, len(fresh)),
        "net_ev_after_spread_fees": {"selected_decisions": co._net_ev(called),
                                     "existing_displayed": co._net_ev(su.current_ai(fresh)[1])},
        "existing_displayed": co._metrics(su.current_ai(fresh)[1], len(fresh)),
        "every_market": {name: co._metrics([(p[r["ticker"]], r) for r in fresh if r["ticker"] in p], len(fresh))
                         for name, p in final.items()},
        "at_operating_threshold": {name: co._metrics(co._called(p, fresh, thr), len(fresh)) for name, p in final.items()},
        "decision_latency_ms": {"p50": round(latencies[len(latencies) // 2], 3),
                                "p95": round(latencies[max(0, int(len(latencies) * 0.95) - 1)], 3),
                                "p99": round(latencies[max(0, int(len(latencies) * 0.99) - 1)], 3),
                                "note": "all specialists + master + gate for one contract, in-process"},
        "recent_decisions": [{"ticker": r["ticker"], **decisions[r["ticker"]], "outcome_up": r["outcome"]}
                             for r in fresh[-10:]],
    }
    report["status"] = "MEASURED"
    return report


def summary(report):
    pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"
    ci = lambda m: "n/a" if not m.get("accuracy_wilson_95") else "-".join(pct(v) for v in m["accuracy_wilson_95"])
    line = lambda label, m: (f"{label}: {m['correct']}/{m['calls']} = {pct(m['accuracy'])} [{ci(m)}], "
                             f"coverage {pct(m['coverage'])} of {m['markets']}")
    d = report["diagnostics"]
    out = [f"FIVE-SPECIALIST OPENING 8768 ({report['format']}) status {report.get('status')}",
           "segments: " + ", ".join(f"{k} {d[k]['contracts']}" for k in ("development", "previous_holdout", "fresh")),
           f"development out-of-fold contracts: {report.get('development_out_of_fold_contracts')}"]
    if report.get("status") != "MEASURED":
        return "\n".join(out)
    out.append(f"selected: {report['selected']}")
    out.append("development log loss (best first): " + ", ".join(
        f"{k} {v:.4f}" for k, v in list(report["development_log_loss"].items())[:8]))
    out.append("specialist value (dev): " + "; ".join(
        f"{k}: {v['verdict']} ({v['delta_log_loss_if_removed']:+.4f})" for k, v in report["specialist_value"].items()))
    g = report["confidence_gate"]
    out.append(f"operating threshold {report['operating_threshold']}; confidence gate cut {g['cut']} used={g['used']}")
    f = report["fresh"]
    if not f.get("contracts"):
        out.append("FRESH: no settled contracts after the boundary yet; rerun later")
        return "\n".join(out)
    out.append(f"FRESH UNSEEN CONTRACTS: {f['contracts']}")
    out.append(line("  SELECTED decisions (UP/DOWN calls)", f["selected_decisions"]))
    out.append(line("  existing AI displayed calls", f["existing_displayed"]))
    for name in ("existing_only", "logistic_only", "combined_mean_existing_aug", "master_full",
                 "master_existing_pattern", "master_specialists_only"):
        if name in f["every_market"]:
            out.append(line(f"  {name} every market", f["every_market"][name]) + " | at threshold: "
                       + line("", f["at_operating_threshold"][name]).lstrip(": "))
    ev = f["net_ev_after_spread_fees"]["selected_decisions"]
    out.append(f"  net EV after spread/fees/slippage: {ev['mean_net_pnl_per_contract']} per contract over "
               f"{ev['calls_with_executable_quote']} priced calls (95% {ev['net_pnl_95']})")
    out.append(f"  decision latency ms: {f['decision_latency_ms']}")
    for dcs in f["recent_decisions"][-3:]:
        out.append(f"  {dcs['ticker']}: {dcs['direction']} ({dcs['reason']}) -> {'UP' if dcs['outcome_up'] else 'DOWN'}")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous-report", type=Path, required=True)
    parser.add_argument("--asof")
    args = parser.parse_args(argv)
    out = args.output.resolve()
    if out.suffix != ".json" or out == args.journal.resolve() or out.name.startswith(args.journal.name):
        parser.error("output must be a separate .json report")
    previous = json.loads(args.previous_report.read_text())
    asof = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    with closing(sqlite3.connect(f"file:{args.journal.resolve()}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        live = research.load(db, asof)
        retro, info = research.retrospective_rows(db, {r["ticker"] for r in live}, limit=1500, asof=asof)
    report = evaluate(live, retro, previous["holdout_starts"], previous["asof"],
                      progress=lambda m: print("SHADOW:", m, flush=True), retro_info=info)
    report["asof"] = asof.isoformat()
    tmp = out.with_suffix(".json.next")
    tmp.write_text(json.dumps(report, indent=1, default=str))
    tmp.replace(out)
    print(summary(report))
    print(f"REPORT {out}")


if __name__ == "__main__":
    main()

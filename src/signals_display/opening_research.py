"""Read-only opening-prediction research: accuracy, opportunities, costs. SHADOW ONLY.

Answers, from a journal (opened read-only), questions such as:

* which model generated each recorded opening and why probabilities sit near 50%;
* whether alternative, regularised models beat the current opening record on
  contracts they never saw (strict expanding walk-forward, label purging);
* which feature groups add out-of-sample value (paired tests, not assumptions);
* how many UP/DOWN calls each confidence threshold produces, how accurate and
  calibrated they are, and whether they would have made money against the
  actual executable Kalshi ask, fees and slippage at the opening call.

Thresholds are chosen on the earlier development part of the out-of-sample
period only; the later holdout part is scored once. Nothing here changes any
model, artifact, registry, journal, live prediction or dashboard, and the
report cannot be loaded as a forecast artifact.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3

from . import learning, opening_models, rigorous_evaluation as rigor
from .calibration import fit_platt
from .model import timestamp
from .scores import score, wilson_interval

REPORT_FORMAT = "kxbtc-opening-research-report-v1"
THRESHOLDS = (0.50, 0.52, 0.55, 0.60, 0.65, 0.70)
SLIPPAGE = 0.01
QUOTE_WINDOW_SECONDS = 60
WALK_START = 170
WALK_BLOCK = 30
MAX_FOLDS = 20
MAX_TRAIN_ROWS = 2000
RESEARCH_LAMBDA_GRID = (3.0, 30.0, 300.0)
DEVELOPMENT_FRACTION = 0.7
MIN_DEVELOPMENT_CALLS = 30

TECHNICALS = ("rsi_14", "macd_scaled", "ema_gap", "vwap_deviation", "bollinger_z", "volatility_24h")
CANDLES = ("upper_wick", "lower_wick", "range_fraction", "candle_body_range_ratio",
           "candle_close_range_position", "candle_volume_weighted_move", "candle_atr_zscore_20",
           "candle_unusual_expansion")
FLOW = ("spot_cvd_delta_fraction", "perpetual_cvd_delta_fraction", "spot_book_dominance",
        "spot_book_imbalance_top3", "spot_micro_price_deviation", "advanced_flow_imbalance",
        "advanced_cvd_slope", "advanced_book_imbalance", "advanced_effective_spread",
        "advanced_trade_intensity", "advanced_vwap_deviation_fraction")
FEATURE_GROUPS = {
    "momentum_acceleration": ("return_1m", "return_5m", "return_15m", "return_60m", "acceleration", "breakout_distance"),
    "technicals_ema_rsi_macd_vwap_bollinger": TECHNICALS,
    "volume": ("relative_volume", "volatility_change"),
    "candles": CANDLES,
    "order_book_flow_cvd": FLOW + ("spot_depth_imbalance",),
    "derivatives": ("derivatives_basis",),
}
MODELS = {
    "compact_offset": dict(features=rigor.COMPACT_FEATURES, offset=True),
    "compact_plus_technicals": dict(features=rigor.COMPACT_FEATURES + TECHNICALS, offset=True),
    "compact_plus_flow": dict(features=rigor.COMPACT_FEATURES + FLOW, offset=True),
    "compact_plus_candles": dict(features=rigor.COMPACT_FEATURES + CANDLES, offset=True),
    "regime_aware_compact": dict(features=rigor.COMPACT_FEATURES + ("regime_rising", "regime_falling"), offset=True),
    "boosted_stumps": dict(features=rigor.COMPACT_FEATURES + TECHNICALS, boost=True),
}


# --------------------------------------------------------------------- loading


def load(db, asof=None):
    """Leakage-checked opening rows plus the recorded live call and the first
    executable quote at or after that call (costs only, never a feature)."""
    asof = asof or datetime.now(timezone.utc)
    rows = opening_models.dataset(db, asof)
    recorded = {
        ticker: (up, payload)
        for ticker, up, payload in db.execute(
            "SELECT ticker,up,payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA'"
        )
    }
    has_predictions = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='predictions'"
    ).fetchone()
    for row in rows:
        up, payload = recorded.get(row["ticker"], (None, None))
        try:
            saved = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            saved = {}
        row["recorded_up"] = up
        row["recorded_direction"] = saved.get("direction")
        row["recorded_model"] = saved.get("model") or "UNRECORDED"
        row["recorded_threshold"] = (saved.get("decision_policy") or {}).get("threshold")
        row["recorded_confidence_status"] = (saved.get("confidence") or {}).get("status")
        regime = str(row.get("regime") or "")
        row["features"] = {
            **row["features"],
            "regime_rising": 1.0 if regime.startswith("rising") else 0.0,
            "regime_falling": 1.0 if regime.startswith("falling") else 0.0,
        }
        row["quote"] = _quote_at(db, row) if has_predictions else None
    return rows


def _quote_at(db, row):
    called = timestamp(row["timestamp"])
    latest = called + timedelta(seconds=QUOTE_WINDOW_SECONDS)
    found = db.execute(
        """SELECT timestamp,json_extract(payload,'$.stale'),
             json_extract(payload,'$.market.quotes.up.bid'),json_extract(payload,'$.market.quotes.up.ask'),
             json_extract(payload,'$.market.quotes.down.ask'),json_extract(payload,'$.market.fee_multiplier')
           FROM predictions WHERE ticker=? AND mode='PUBLIC MARKET DATA'
             AND julianday(timestamp)>=julianday(?) AND julianday(timestamp)<=julianday(?)
           ORDER BY timestamp LIMIT 5""",
        (row["ticker"], called.isoformat(), latest.isoformat()),
    ).fetchall()
    for at, stale, up_bid, up_ask, down_ask, multiplier in found:
        values = (up_ask, down_ask, multiplier)
        if stale or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            continue
        if not (0 < up_ask < 1 and 0 < down_ask < 1):
            continue
        mid = (up_bid + up_ask) / 2 if type(up_bid) in (int, float) and 0 <= up_bid <= up_ask else None
        return {"observed_at": at, "up_ask": up_ask, "down_ask": down_ask,
                "fee_multiplier": multiplier, "up_mid": mid}
    return None


# --------------------------------------------------------------- models / folds


def _fit(train, validation, spec):
    names = [n for n in spec["features"] if any(r["features"].get(n) is not None for r in train)]
    reduced = lambda rows: [{**r, "features": {k: r["features"].get(k) for k in names}} for r in rows]
    prep = learning.preprocess(reduced(train))
    if not prep["names"]:
        raise ValueError("no usable features")
    if spec.get("boost"):
        model = opening_models.fit_boost(train, prep, rounds=30)
        raw = lambda row: opening_models._boost_raw(model, row["features"])
    else:
        best = None
        size = len(train) // 4
        for l2 in RESEARCH_LAMBDA_GRID:  # expanding time-series CV inside train only
            losses = []
            for fold in range(1, 4):
                fit_rows, held = train[: fold * size], train[fold * size: (fold + 1) * size]
                start = min(timestamp(r["timestamp"]) for r in held)
                fit_rows = [r for r in fit_rows if timestamp(r["label_available_at"]) <= start]
                if len(fit_rows) < 20:
                    continue
                m = rigor.fit_logistic(fit_rows, prep, l2, use_offset=spec["offset"])
                losses += rigor._log_losses([rigor.raw_probability(m, r) for r in held], [r["outcome"] for r in held])
            if losses and (best is None or sum(losses) / len(losses) < best[0]):
                best = (sum(losses) / len(losses), l2)
        model = rigor.fit_logistic(train, prep, best[1] if best else 10.0, use_offset=spec["offset"])
        raw = lambda row: rigor.raw_probability(model, row)
    platt = fit_platt([raw(r) for r in validation], [r["outcome"] for r in validation])
    return lambda row: learning.sigmoid(platt["slope"] * learning.logit(raw(row)) + platt["intercept"])


def walk_forward(rows, start=WALK_START, block=WALK_BLOCK, progress=None):
    """Expanding chronological folds. Every prediction is for a contract whose
    label was unavailable to training and calibration (purged by label time).
    Fold size grows with the journal so at most MAX_FOLDS refits run; the
    training window is bounded to the most recent MAX_TRAIN_ROWS contracts."""
    rows = opening_models._validate_rows(rows)
    block = max(block, math.ceil(max(0, len(rows) - start) / MAX_FOLDS))
    predictions = {name: {} for name in MODELS}
    folds = 0
    for end in range(start, len(rows), block):
        past, test = rows[max(0, end - MAX_TRAIN_ROWS):end], rows[end: end + block]
        split = int(len(past) * 0.8)
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 50 or len(validation) < 15 or not test:
            continue
        if min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10:
            continue
        folds += 1
        if progress:
            progress(f"fold {folds}: train<= {end} contracts, testing {len(test)}")
        for name, spec in MODELS.items():
            try:
                predict = _fit(train, validation, spec)
            except (ValueError, ZeroDivisionError):
                continue
            for row in test:
                predictions[name][row["ticker"]] = predict(row)
    return predictions, folds


# --------------------------------------------------------------- measurement


def _cost(row, side):
    quote = row.get("quote")
    if not quote:
        return None
    from .edge_scanner import quantity_fee

    ask = quote["up_ask"] if side == "up" else quote["down_ask"]
    cost = ask + quantity_fee(ask, 1, quote["fee_multiplier"]) + SLIPPAGE
    return cost if cost < 1 else None


def call_metrics(pairs):
    """pairs: (probability_up, row) for the called contracts."""
    n = len(pairs)
    correct = sum((p >= 0.5) == bool(r["outcome"]) for p, r in pairs)
    pnl, expected = [], []
    for p, r in pairs:
        side = "up" if p >= 0.5 else "down"
        cost = _cost(r, side)
        if cost is None:
            continue
        won = r["outcome"] == (1 if side == "up" else 0)
        pnl.append((1.0 if won else 0.0) - cost)
        expected.append(max(p, 1 - p) - cost)
    mean = sum(pnl) / len(pnl) if pnl else None
    half = (1.96 * math.sqrt(sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1) / len(pnl))
            if len(pnl) > 1 else None)
    probabilistic = score([p for p, _ in pairs], [r["outcome"] for _, r in pairs]) if pairs else {}
    return {
        "calls": n,
        "correct": correct,
        "wrong": n - correct,
        "accuracy": correct / n if n else None,
        "accuracy_wilson_95": wilson_interval(correct, n),
        "mean_confidence": sum(max(p, 1 - p) for p, _ in pairs) / n if n else None,
        "brier": probabilistic.get("brier"),
        "log_loss": probabilistic.get("log_loss"),
        "calls_with_executable_quote": len(pnl),
        "mean_net_pnl_per_contract": mean,
        "net_pnl_95": [mean - half, mean + half] if half is not None else None,
        "total_net_pnl_one_contract_each": sum(pnl) if pnl else None,
        "mean_model_expected_value": sum(expected) / len(expected) if expected else None,
    }


def threshold_table(probabilities, rows):
    table = {}
    for threshold in THRESHOLDS:
        pairs = [
            (p, r) for p, r in zip(probabilities, rows)
            if (max(p, 1 - p) > 0.5 if threshold == 0.5 else max(p, 1 - p) >= threshold)
        ]
        table[f"{threshold:.2f}"] = {**call_metrics(pairs), "coverage": len(pairs) / len(rows) if rows else 0}
    return table


def _choose_threshold(probabilities, rows):
    """Development-only choice: best mean net P&L with enough priced calls."""
    best = None
    for key, metrics in threshold_table(probabilities, rows).items():
        if metrics["calls_with_executable_quote"] < MIN_DEVELOPMENT_CALLS:
            continue
        candidate = (metrics["mean_net_pnl_per_contract"], -float(key), float(key))
        if best is None or candidate > best:
            best = candidate
    return best[2] if best else None


def research(rows, progress=None):
    rows = opening_models._validate_rows(rows)
    report = {
        "format": REPORT_FORMAT,
        "role": "SHADOW_RESEARCH_ONLY",
        "promotion_allowed": False,
        "activation_allowed": False,
        "active_model_changed": False,
        "eligible_opening_contracts": len(rows),
        "costs": {"slippage_per_contract": SLIPPAGE, "fees": "Kalshi taker fee formula", "quote": f"first non-stale executable ask within {QUOTE_WINDOW_SECONDS}s after the opening call"},
    }
    recorded = [r for r in rows if type(r.get("recorded_up")) in (int, float)]
    confidences = sorted(abs(r["recorded_up"] - 0.5) for r in recorded)
    report["recorded_live_openings"] = {
        "contracts": len(recorded),
        "by_model": _count(r["recorded_model"] for r in recorded),
        "decision_thresholds_seen": _count(str(r["recorded_threshold"]) for r in recorded),
        "confidence_status_seen": _count(str(r["recorded_confidence_status"]) for r in recorded),
        "median_confidence": 0.5 + confidences[len(confidences) // 2] if confidences else None,
        "p90_confidence": 0.5 + confidences[int(len(confidences) * 0.9)] if confidences else None,
        "share_at_or_above_55": sum(c >= 0.05 for c in confidences) / len(confidences) if confidences else None,
        "saved_calls": _count(str(r["recorded_direction"]) for r in recorded),
        "threshold_table": threshold_table([r["recorded_up"] for r in recorded], recorded),
    }
    report["feature_coverage_at_opening"] = {
        group: {name: sum(r["features"].get(name) is not None for r in rows) / len(rows) if rows else 0 for name in names}
        for group, names in FEATURE_GROUPS.items()
    }
    predictions, folds = walk_forward(rows, progress=progress)
    report["walk_forward_folds"] = folds
    oos = [r for r in rows if all(r["ticker"] in predictions[name] for name in ("compact_offset",))]
    report["out_of_sample_contracts"] = len(oos)
    if len(oos) < 2 * MIN_DEVELOPMENT_CALLS:
        report["status"] = "INSUFFICIENT_OUT_OF_SAMPLE_CONTRACTS"
        report["reason"] = f"Need roughly {WALK_START + 2 * MIN_DEVELOPMENT_CALLS}+ eligible frozen openings for a walk-forward comparison"
        return report
    sources = {
        "opening_statistical_baseline": {r["ticker"]: r["baseline"] for r in oos},
        "recorded_live_opening": {r["ticker"]: r["recorded_up"] for r in oos if type(r.get("recorded_up")) in (int, float)},
        "kalshi_market_mid_at_call": {r["ticker"]: r["quote"]["up_mid"] for r in oos if r.get("quote") and r["quote"].get("up_mid") is not None},
        **predictions,
    }
    split = int(len(oos) * DEVELOPMENT_FRACTION)
    development, holdout = oos[:split], oos[split:]
    comparison = {}
    for name, by_ticker in sources.items():
        dev = [r for r in development if r["ticker"] in by_ticker]
        hold = [r for r in holdout if r["ticker"] in by_ticker]
        if not dev or not hold:
            continue
        chosen = _choose_threshold([by_ticker[r["ticker"]] for r in dev], dev)
        hold_p = [by_ticker[r["ticker"]] for r in hold]
        entry = {
            "development_contracts": len(dev),
            "holdout_contracts": len(hold),
            "holdout_probabilistic": {k: score(hold_p, [r["outcome"] for r in hold])[k] for k in ("brier", "log_loss", "accuracy")},
            "threshold_table_holdout": threshold_table(hold_p, hold),
            "threshold_chosen_on_development": chosen,
        }
        if chosen is not None:
            picked = [(p, r) for p, r in zip(hold_p, hold) if max(p, 1 - p) >= chosen]
            entry["holdout_at_chosen_threshold"] = call_metrics(picked)
            entry["holdout_by_regime"] = {
                regime: call_metrics([(p, r) for p, r in picked if r.get("regime") == regime])
                for regime in sorted({r.get("regime") for r in hold})
            }
        base = [r for r in hold if r["ticker"] in sources["opening_statistical_baseline"]]
        if name != "opening_statistical_baseline" and base:
            entry["holdout_log_loss_vs_baseline"] = rigor.paired_superiority_test(
                rigor._log_losses([by_ticker[r["ticker"]] for r in base], [r["outcome"] for r in base]),
                rigor._log_losses([r["baseline"] for r in base], [r["outcome"] for r in base]),
            )
        comparison[name] = entry
    report["model_comparison"] = comparison
    ablation = {}
    reference = predictions.get("compact_offset", {})
    for name in ("compact_plus_technicals", "compact_plus_flow", "compact_plus_candles", "regime_aware_compact"):
        common = [r for r in oos if r["ticker"] in reference and r["ticker"] in predictions.get(name, {})]
        if common:
            ablation[name] = rigor.paired_superiority_test(
                rigor._log_losses([predictions[name][r["ticker"]] for r in common], [r["outcome"] for r in common]),
                rigor._log_losses([reference[r["ticker"]] for r in common], [r["outcome"] for r in common]),
            )
    report["incremental_value_vs_compact"] = ablation
    report["holdout_detectable_accuracy_margin"] = 1.96 * math.sqrt(0.25 / len(holdout)) if holdout else None
    report["status"] = "RESEARCH_COMPLETE"
    report["note"] = "Out-of-sample = walk-forward predictions on contracts unseen by training/calibration. Thresholds chosen on development only; holdout scored once. Not a profitability guarantee."
    return report


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))


def summary_text(report):
    lines = [f"Opening research ({report.get('status')}): {report['eligible_opening_contracts']} eligible openings"]
    live = report.get("recorded_live_openings", {})
    lines.append(f"Recorded live models: {live.get('by_model')}  median confidence: {live.get('median_confidence')}")
    for name, entry in (report.get("model_comparison") or {}).items():
        chosen = entry.get("holdout_at_chosen_threshold") or {}
        lines.append(
            f"{name:34s} holdout logloss={entry['holdout_probabilistic']['log_loss']:.4f} "
            f"threshold={entry.get('threshold_chosen_on_development')} calls={chosen.get('calls')} "
            f"acc={chosen.get('accuracy')} net/contract={chosen.get('mean_net_pnl_per_contract')}"
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("opening_research.report.json"))
    args = parser.parse_args()
    try:
        rigor._refuse_foreign_file(args.output, REPORT_FORMAT)
        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            rows = load(db)
        print(f"Loaded {len(rows)} eligible frozen openings (read-only)", flush=True)
        report = research(rows, progress=lambda m: print("RESEARCH_PROGRESS " + m, flush=True))
        from .model_registry import atomic_json

        atomic_json(args.output, report)
        print(summary_text(report))
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

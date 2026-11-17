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
import bisect
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import time

import hashlib

from . import learning, opening_models, research_fast as fast, rigorous_evaluation as rigor, settled_audit
from .calibration import fit_platt
from .model import timestamp
from .scores import score, wilson_interval

REPORT_FORMAT = "kxbtc-opening-research-report-v1"
THRESHOLDS = (0.50, 0.52, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
RETROSPECTIVE_LIMIT = 3000
SLIPPAGE = 0.01
QUOTE_WINDOW_SECONDS = 60
PREOPEN_QUOTE_WINDOW_SECONDS = 60
WALK_START = 170
WALK_BLOCK = 30
MAX_FOLDS = 20
MAX_TRAIN_ROWS = 2000
RESEARCH_LAMBDA_GRID = (3.0, 30.0, 300.0)
SEALED_HOLDOUT_FRACTION = 0.20  # docs/RESEARCH_PLAN_SETTLED_ACCURACY.md section 4.1
MIN_DEVELOPMENT_CALLS = 30
MIN_FAMILY_CONTRACTS = 300  # plan section 5: below this a family is INSUFFICIENT_HISTORY

TECHNICALS = ("rsi_14", "macd_scaled", "ema_gap", "vwap_deviation", "bollinger_z", "volatility_24h")
CANDLES = ("upper_wick", "lower_wick", "range_fraction", "candle_body_range_ratio",
           "candle_close_range_position", "candle_volume_weighted_move", "candle_atr_zscore_20",
           "candle_unusual_expansion")
FLOW = ("spot_cvd_delta_fraction", "perpetual_cvd_delta_fraction", "spot_book_dominance",
        "spot_book_imbalance_top3", "spot_micro_price_deviation", "advanced_flow_imbalance",
        "advanced_cvd_slope", "advanced_book_imbalance", "advanced_effective_spread",
        "advanced_trade_intensity", "advanced_vwap_deviation_fraction")
TREND = ("opening_ema9_distance", "opening_ema9_21_gap", "opening_ema50_distance",
         "opening_ema200_distance", "opening_volume_acceleration")
FEATURE_GROUPS = {
    "momentum_acceleration": ("return_1m", "return_5m", "return_15m", "return_60m", "acceleration", "breakout_distance"),
    "technicals_ema_rsi_macd_vwap_bollinger": TECHNICALS,
    "volume": ("relative_volume", "volatility_change"),
    "candles": CANDLES,
    "order_book_flow_cvd": FLOW + ("spot_depth_imbalance",),
    "derivatives": ("derivatives_basis",),
    "trend_ema_volume_acceleration": TREND,
    "kalshi_preopen_market": ("kalshi_preopen_mid_logit",),
}
# Six pre-registered fitted candidates.  Regime slices and Kalshi prices are
# references/conditions, not extra candidates added after results are visible.
MODELS = {
    "compact_offset": dict(features=rigor.COMPACT_FEATURES, offset=True),
    "compact_plus_technicals": dict(features=rigor.COMPACT_FEATURES + TECHNICALS, offset=True),
    "compact_plus_flow": dict(features=rigor.COMPACT_FEATURES + FLOW, offset=True),
    "compact_plus_candles": dict(features=rigor.COMPACT_FEATURES + CANDLES, offset=True),
    # Integrated Phase 4 slot (Claude + Codex): the trend family together
    # with volatility-regime interactions in ONE pre-registered candidate, so
    # the family stays at six. Interactions are products of frozen
    # as-of-opening values only.
    "momentum_trend_volatility": dict(features=rigor.COMPACT_FEATURES + TREND + (
        "regime_rising", "regime_falling", "ix_return_15m_x_rising", "ix_return_15m_x_falling",
        "ix_ema9_21_gap_x_rising", "ix_ema9_21_gap_x_falling"), offset=True),
    "boosted_stumps": dict(features=rigor.COMPACT_FEATURES + TECHNICALS, boost=True),
}
PREREGISTERED_CANDIDATE_LIMIT = 6
assert len(MODELS) <= PREREGISTERED_CANDIDATE_LIMIT, "pre-registered candidate family exceeded"
INTERACTIONS = {
    "ix_return_15m_x_rising": ("return_15m", "regime_rising"),
    "ix_return_15m_x_falling": ("return_15m", "regime_falling"),
    "ix_ema9_21_gap_x_rising": ("opening_ema9_21_gap", "regime_rising"),
    "ix_ema9_21_gap_x_falling": ("opening_ema9_21_gap", "regime_falling"),
}


def add_interactions(rows):
    """Products of two frozen opening values; missing if either input is missing."""
    for row in rows:
        f = row["features"]
        for name, (a, b) in INTERACTIONS.items():
            x, y = f.get(a), f.get(b)
            f[name] = (float(x) * float(y)
                       if type(x) in (int, float) and type(y) in (int, float)
                       and math.isfinite(x) and math.isfinite(y) else None)
    return rows


# --------------------------------------------------------------------- loading


def load(db, asof=None):
    """Leakage-checked opening rows plus the recorded live call and the first
    executable quote at or after that call (costs only, never a feature)."""
    asof = asof or datetime.now(timezone.utc)
    rows = opening_models.dataset(db, asof)
    # Defensive quarantine (matches settled_audit.official_outcomes): a ticker
    # joined to more than one official row is dropped, never trained or
    # scored twice and never given whichever label SQLite returned.
    counts = {}
    for row in rows:
        counts[row["ticker"]] = counts.get(row["ticker"], 0) + 1
    rows = [row for row in rows if counts[row["ticker"]] == 1]
    recorded = {
        ticker: (up, payload)
        for ticker, up, payload in db.execute(
            "SELECT ticker,up,payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA'"
        )
    }
    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_predictions = "predictions" in tables
    has_snapshots = "opening_quote_snapshots" in tables
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
        row["quote"] = (_snapshot_quote_at(db, row) if has_snapshots else None) or (
            _quote_at(db, row) if has_predictions else None
        )
        preopen = _preopen_mid(db, row) if has_snapshots else None
        row["kalshi_preopen_mid"] = preopen
        if preopen is not None:
            row["features"]["kalshi_preopen_mid_logit"] = learning.logit(preopen)
    return rows


ASSUMED_FEE_MULTIPLIER = 1.0  # Kalshi standard taker schedule; labelled when used


def _preopen_mid(db, row):
    """Latest fresh Kalshi UP mid received no later than the market opening.

    The immutable call can be generated after the opening while the server
    recovers its receipt-proven snapshot.  Using the call time here would then
    admit post-opening prices into a feature advertised as pre-opening.
    """
    opened = timestamp(row["opens_at"])
    found = db.execute(
        """SELECT up_bid,up_ask FROM opening_quote_snapshots
           WHERE ticker=? AND mode='PUBLIC MARKET DATA'
             AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?)
             AND up_bid IS NOT NULL AND up_ask IS NOT NULL
           ORDER BY observed_at DESC LIMIT 1""",
        (
            row["ticker"],
            (opened - timedelta(seconds=PREOPEN_QUOTE_WINDOW_SECONDS)).isoformat(),
            opened.isoformat(),
        ),
    ).fetchone()
    if not found:
        return None
    bid, ask = found
    if not (0 < bid <= ask < 1):
        return None
    return (bid + ask) / 2


def _snapshot_quote_at(db, row):
    """First recorded opening quote at or after the call (opening_quote_snapshots)."""
    called = timestamp(row["timestamp"])
    found = db.execute(
        """SELECT observed_at,up_bid,up_ask,down_ask,fee_multiplier FROM opening_quote_snapshots
           WHERE ticker=? AND mode='PUBLIC MARKET DATA'
             AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?)
           ORDER BY observed_at LIMIT 1""",
        (row["ticker"], called.isoformat(), (called + timedelta(seconds=QUOTE_WINDOW_SECONDS)).isoformat()),
    ).fetchone()
    if not found:
        return None
    at, up_bid, up_ask, down_ask, multiplier = found
    if any(type(value) not in (int, float) or not math.isfinite(value)
           for value in (up_ask, down_ask)):
        return None
    if not (0 < up_ask < 1 and 0 < down_ask < 1):
        return None
    assumed = type(multiplier) not in (int, float) or not math.isfinite(multiplier)
    mid = (up_bid + up_ask) / 2 if type(up_bid) in (int, float) and 0 <= up_bid <= up_ask else None
    return {"observed_at": at, "up_ask": up_ask, "down_ask": down_ask, "up_mid": mid,
            "fee_multiplier": ASSUMED_FEE_MULTIPLIER if assumed else multiplier,
            "fee_multiplier_assumed": assumed, "source": "opening_quote_snapshots"}


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
        if stale or any(type(v) not in (int, float) or not math.isfinite(v) for v in (up_ask, down_ask)):
            continue
        if not (0 < up_ask < 1 and 0 < down_ask < 1):
            continue
        # Older compact evidence did not keep the fee schedule; the standard
        # multiplier is then assumed and labelled, never silently treated as free.
        assumed = type(multiplier) not in (int, float) or not math.isfinite(multiplier)
        mid = (up_bid + up_ask) / 2 if type(up_bid) in (int, float) and 0 <= up_bid <= up_ask else None
        return {"observed_at": at, "up_ask": up_ask, "down_ask": down_ask, "up_mid": mid,
                "fee_multiplier": ASSUMED_FEE_MULTIPLIER if assumed else multiplier,
                "fee_multiplier_assumed": assumed, "source": "predictions"}
    return None


# --------------------------------------------------------------- models / folds


def _preprocess(rows, names):
    """learning.preprocess semantics over an explicit name list. learning's own
    version iterates the fixed FEATURES vocabulary and would silently drop
    research-only inputs such as regime indicators or pre-open market prices."""
    prep = {"names": [], "means": [], "scales": []}
    for name in names:
        values = [r["features"].get(name) for r in rows]
        values = [float(v) for v in values if type(v) in (int, float) and math.isfinite(v)]
        if len(values) < len(rows) / 2:
            continue
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / len(values)
        if var < 1e-12:
            continue
        prep["names"].append(name)
        prep["means"].append(mean)
        prep["scales"].append(math.sqrt(var))
    return prep


def _fit(train, validation, spec):
    names = [n for n in spec["features"] if any(r["features"].get(n) is not None for r in train)]
    prep = _preprocess(train, names)
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
                # Scaling is learned from each fold's past-only fit rows.  The
                # final preprocessor above is valid for the final train fit,
                # but reusing it here would leak later feature distributions
                # into the inner hyperparameter selection.
                fold_prep = _preprocess(fit_rows, names)
                if not fold_prep["names"]:
                    continue
                m = fast.fit_logistic(fit_rows, fold_prep, l2, use_offset=spec["offset"])
                losses += rigor._log_losses([rigor.raw_probability(m, r) for r in held], [r["outcome"] for r in held])
            if losses and (best is None or sum(losses) / len(losses) < best[0]):
                best = (sum(losses) / len(losses), l2)
        model = fast.fit_logistic(train, prep, best[1] if best else 10.0, use_offset=spec["offset"])
        raw = lambda row: rigor.raw_probability(model, row)
    platt = fit_platt([raw(r) for r in validation], [r["outcome"] for r in validation])
    return lambda row: learning.sigmoid(platt["slope"] * learning.logit(raw(row)) + platt["intercept"])


def walk_forward(rows, start=WALK_START, block=WALK_BLOCK, progress=None, seal=None, checkpoint=None):
    """Expanding chronological folds. Every prediction is for a contract whose
    label was unavailable to training and calibration (purged by label time).
    Fold size grows with the journal so at most MAX_FOLDS refits run; the
    training window is bounded to the most recent MAX_TRAIN_ROWS contracts.

    With ``seal`` (an index), folds run only on the development rows before
    it. Every model is then fitted ONCE on development rows and frozen, and
    that frozen model scores the whole sealed holdout. No holdout label ever
    enters training, calibration, hyperparameter or threshold selection.

    With ``checkpoint`` (research_fast.Checkpoints), each (fold, model) result
    is stored under a hash of its exact inputs and reused on a resumed run;
    the fold boundaries, purging and fitting are unchanged."""
    rows = opening_models._validate_rows(rows)
    limit = len(rows) if seal is None else seal
    block = max(block, math.ceil(max(0, limit - start) / MAX_FOLDS))
    predictions = {name: {} for name in MODELS}
    folds = 0
    ends = list(range(start, limit, block))
    if seal is not None and seal < len(rows):
        ends.append(seal)
    for end in ends:
        sealed_fold = seal is not None and end == seal
        stop = len(rows) if sealed_fold else min(end + block, limit)
        past, test = rows[max(0, end - MAX_TRAIN_ROWS):end], rows[end:stop]
        split = int(len(past) * 0.8)
        train, validation, test = opening_models._purge(past[:split], past[split:], test)
        if len(train) < 50 or len(validation) < 15 or not test:
            continue
        if min(sum(r["outcome"] == y for r in train) for y in (0, 1)) < 10:
            continue
        folds += 1
        if progress:
            label = "SEALED HOLDOUT (frozen development model)" if sealed_fold else f"fold {folds}"
            progress(f"{label}: train<= {end} contracts, testing {len(test)}")
        started = time.monotonic()
        reused = 0
        for name, spec in MODELS.items():
            key = checkpoint.key(name, spec, train, validation, test) if checkpoint else None
            saved = checkpoint.get(key) if checkpoint else None
            if saved is not None:
                reused += 1
                predictions[name].update(saved.get("predictions") or {})
                continue
            try:
                predict = _fit(train, validation, spec)
            except (ValueError, ZeroDivisionError) as exc:
                if checkpoint:
                    checkpoint.put(key, name, failed=type(exc).__name__)
                continue
            fold_predictions = {row["ticker"]: predict(row) for row in test}
            predictions[name].update(fold_predictions)
            if checkpoint:
                checkpoint.put(key, name, predictions=fold_predictions)
        if progress:
            progress(f"  fold {folds} done in {time.monotonic() - started:.1f}s "
                     f"({reused}/{len(MODELS)} models reused from checkpoint)")
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
    pnl, expected, priced = [], [], []
    for p, r in pairs:
        side = "up" if p >= 0.5 else "down"
        cost = _cost(r, side)
        if cost is None:
            continue
        won = r["outcome"] == (1 if side == "up" else 0)
        pnl.append((1.0 if won else 0.0) - cost)
        expected.append(max(p, 1 - p) - cost)
        priced.append({"day": str(r.get("opens_at") or r.get("timestamp"))[:10], "pnl": pnl[-1]})
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
        "net_pnl_day_block_bootstrap_95": day_block_bootstrap(priced) if priced else None,
        "max_drawdown": _max_drawdown(pnl) if pnl else None,
    }


def _max_drawdown(pnl):
    total = peak = worst = 0.0
    for x in pnl:
        total += x
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def threshold_table(probabilities, rows):
    table = {}
    for threshold in THRESHOLDS:
        pairs = [
            (p, r) for p, r in zip(probabilities, rows)
            if (max(p, 1 - p) > 0.5 if threshold == 0.5 else max(p, 1 - p) >= threshold)
        ]
        table[f"{threshold:.2f}"] = {
            **call_metrics(pairs),
            "up_calls": sum(p >= 0.5 for p, _ in pairs),
            "down_calls": sum(p < 0.5 for p, _ in pairs),
            "wait": len(rows) - len(pairs),
            "coverage": len(pairs) / len(rows) if rows else 0,
        }
    return table


DISPLAYABLE_RECORDED_CONFIDENCE = {
    "CALIBRATED", "EXPERIMENTAL CALIBRATED CANDIDATE"
}


def recorded_decision_metrics(rows):
    """Score only the immutable UP/DOWN decisions actually displayed live.

    Counterfactual probability cutoffs remain in ``threshold_table``; they
    must not be confused with the bot's historical call record.
    """
    pairs, waits, invalid = [], 0, 0
    for row in rows:
        direction = row.get("recorded_direction")
        status = row.get("recorded_confidence_status")
        probability = row.get("recorded_up")
        if direction not in {"UP", "DOWN"} or status not in DISPLAYABLE_RECORDED_CONFIDENCE:
            waits += 1
            continue
        if (
            type(probability) not in (int, float)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
            or (direction == "UP") != (probability >= 0.5)
        ):
            invalid += 1
            continue
        pairs.append((probability, row))
    return {**call_metrics(pairs), "wait_or_withheld": waits, "invalid_direction_records": invalid}


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


CONDITION_FEATURES = ("return_15m", "return_60m", "opening_ema50_distance", "opening_ema9_21_gap", "rsi_14",
                      "bollinger_z", "candle_atr_zscore_20", "relative_volume", "volatility_change",
                      "body_fraction", "breakout_distance")


def condition_report(rows):
    """EXPLORATORY: settled UP rate by tercile of key chart features, with Wilson
    intervals. Descriptive only (many conditions are scanned, so some will look
    significant by chance); never used to select a model or threshold."""
    report = {}
    for name in CONDITION_FEATURES:
        pairs = sorted((r["features"][name], r["outcome"]) for r in rows
                       if type(r["features"].get(name)) in (int, float))
        if len(pairs) < 60:
            continue
        third = len(pairs) // 3
        groups = {"low": pairs[:third], "mid": pairs[third:2 * third], "high": pairs[2 * third:]}
        report[name] = {
            label: {"contracts": len(g), "up_rate": sum(y for _, y in g) / len(g),
                    "up_rate_wilson_95": wilson_interval(sum(y for _, y in g), len(g)),
                    "feature_range": [g[0][0], g[-1][0]]}
            for label, g in groups.items()
        }
    by_regime = {}
    for r in rows:
        group = by_regime.setdefault(str(r.get("regime")), [0, 0])
        group[0] += 1
        group[1] += r["outcome"]
    report["volatility_regime"] = {k: {"contracts": n, "up_rate": up / n, "up_rate_wilson_95": wilson_interval(up, n)}
                                   for k, (n, up) in by_regime.items() if n}
    report["note"] = "Exploratory and descriptive; multiple comparisons are not corrected here."
    return report


def _mcnemar(candidate_right, current_right):
    """Exact two-sided McNemar test on paired right/wrong outcomes."""
    cand_only = sum(x and not y for x, y in zip(candidate_right, current_right))
    curr_only = sum(y and not x for x, y in zip(candidate_right, current_right))
    n = cand_only + curr_only
    if not n:
        return {"candidate_only_right": 0, "current_only_right": 0, "p_value": 1.0}
    tail = sum(math.comb(n, i) for i in range(0, min(cand_only, curr_only) + 1)) / 2 ** n
    return {"candidate_only_right": cand_only, "current_only_right": curr_only, "p_value": min(1.0, 2 * tail),
            "test": "exact two-sided McNemar on discordant contracts"}


def current_model_comparison(holdout, predictions, current_model):
    """Candidates versus the RUNNING model on the same sealed-holdout contracts.

    Only openings the live engine recorded as made by ``current_model`` are
    used, so no other model version is mixed in. Two views:
    * same contracts: each candidate's direction on exactly the contracts the
      running model displayed as UP/DOWN (paired McNemar test);
    * matched coverage: each candidate may choose its own k most confident
      contracts, k = the running model's number of displayed calls, so a
      candidate cannot look better simply by calling fewer contracts.
    """
    mine = [r for r in holdout if r.get("recorded_model") == current_model
            and type(r.get("recorded_up")) in (int, float)]
    called = [r for r in mine if r.get("recorded_direction") in {"UP", "DOWN"}
              and r.get("recorded_confidence_status") in DISPLAYABLE_RECORDED_CONFIDENCE]
    out = {"current_model_label": current_model, "holdout_openings_by_current_model": len(mine),
           "current_model_displayed_calls": len(called)}
    if not called:
        out["status"] = "NO_CURRENT_MODEL_CALLS_IN_HOLDOUT"
        return out
    current_right = [(r["recorded_direction"] == "UP") == bool(r["outcome"]) for r in called]
    out["current_model"] = {**call_metrics([(r["recorded_up"], r) for r in called]),
                            "coverage": len(called) / len(mine)}
    out["candidates"] = {}
    for name, by_ticker in predictions.items():
        if not all(r["ticker"] in by_ticker for r in mine):
            continue
        same = [(by_ticker[r["ticker"]], r) for r in called]
        candidate_right = [(p >= 0.5) == bool(r["outcome"]) for p, r in same]
        ranked = sorted(mine, key=lambda r: -abs(by_ticker[r["ticker"]] - 0.5))[:len(called)]
        out["candidates"][name] = {
            "same_contracts": {**call_metrics(same), "paired_vs_current": _mcnemar(candidate_right, current_right)},
            "matched_coverage": call_metrics([(by_ticker[r["ticker"]], r) for r in ranked]),
        }
    out["status"] = "MEASURED"
    return out


def research(rows, progress=None, checkpoint=None, current_model=None):
    rows = add_interactions(opening_models._validate_rows(rows))
    report = {
        "format": REPORT_FORMAT,
        "role": "SHADOW_RESEARCH_ONLY",
        "promotion_allowed": False,
        "activation_allowed": False,
        "active_model_changed": False,
        "eligible_opening_contracts": len(rows),
        "costs": {"slippage_per_contract": SLIPPAGE, "fees": "Kalshi taker fee formula", "quote": f"first non-stale executable ask within {QUOTE_WINDOW_SECONDS}s after the opening call"},
    }
    numeric_recorded = [r for r in rows if type(r.get("recorded_up")) in (int, float)]
    recorded = [r for r in numeric_recorded if math.isfinite(r["recorded_up"])
                and 0 <= r["recorded_up"] <= 1]
    confidences = sorted(abs(r["recorded_up"] - 0.5) for r in recorded)
    report["recorded_live_openings"] = {
        "contracts": len(recorded),
        "invalid_probability_records": len(numeric_recorded) - len(recorded),
        "by_model": _count(r["recorded_model"] for r in recorded),
        "decision_thresholds_seen": _count(str(r["recorded_threshold"]) for r in recorded),
        "confidence_status_seen": _count(str(r["recorded_confidence_status"]) for r in recorded),
        "median_confidence": 0.5 + confidences[len(confidences) // 2] if confidences else None,
        "p90_confidence": 0.5 + confidences[int(len(confidences) * 0.9)] if confidences else None,
        "share_at_or_above_55": sum(c >= 0.05 for c in confidences) / len(confidences) if confidences else None,
        "saved_calls": _count(str(r["recorded_direction"]) for r in recorded),
        "actual_displayed_decisions": recorded_decision_metrics(recorded),
        "threshold_table_scope": "Counterfactual probability cutoffs; not historical displayed calls",
        "threshold_table": threshold_table([r["recorded_up"] for r in recorded], recorded),
    }
    report["conditions_exploratory"] = condition_report(rows)
    failures = [
        r for r in recorded
        if r.get("recorded_direction") in {"UP", "DOWN"}
        and r.get("recorded_confidence_status") in DISPLAYABLE_RECORDED_CONFIDENCE
        and (r["recorded_direction"] == "UP") == (r["recorded_up"] >= 0.5)
        and (r["recorded_direction"] == "UP") != bool(r["outcome"])
    ]
    report["recorded_live_openings"]["failed_calls"] = [
        {"ticker": r["ticker"], "direction": r["recorded_direction"],
         "confidence": max(r["recorded_up"], 1 - r["recorded_up"]), "regime": r.get("regime"),
         "model": r["recorded_model"]}
        for r in sorted(failures, key=lambda r: -max(r["recorded_up"], 1 - r["recorded_up"]))[:20]
    ]
    report["feature_coverage_at_opening"] = {
        group: {name: sum(r["features"].get(name) is not None for r in rows) / len(rows) if rows else 0 for name in names}
        for group, names in FEATURE_GROUPS.items()
    }
    report["feature_family_history"] = feature_family_history(rows)
    seal = len(rows) - int(round(len(rows) * SEALED_HOLDOUT_FRACTION))
    sealed_tickers = [r["ticker"] for r in rows[seal:]]
    report["sealed_holdout"] = {
        "method": f"last {SEALED_HOLDOUT_FRACTION:.0%} of eligible contracts by opening time; models frozen on development rows",
        "seal_index": seal,
        "holdout_contracts": len(sealed_tickers),
        "holdout_starts_at": rows[seal]["opens_at"] if seal < len(rows) else None,
        "holdout_ticker_sha256": hashlib.sha256("\n".join(sealed_tickers).encode()).hexdigest(),
        "tuning_on_holdout": False,
    }
    predictions, folds = walk_forward(rows, progress=progress, seal=seal, checkpoint=checkpoint)
    report["walk_forward_folds"] = folds
    oos = [r for r in rows if all(r["ticker"] in predictions[name] for name in ("compact_offset",))]
    report["out_of_sample_contracts"] = len(oos)
    if len(oos) < 2 * MIN_DEVELOPMENT_CALLS:
        report["status"] = "INSUFFICIENT_OUT_OF_SAMPLE_CONTRACTS"
        report["reason"] = f"Need roughly {WALK_START + 2 * MIN_DEVELOPMENT_CALLS}+ eligible frozen openings for a walk-forward comparison"
        return report
    history = [(timestamp(r["label_available_at"]), r["outcome"]) for r in rows]
    as_audit = [{"ticker": r["ticker"], "opens_at": timestamp(r["opens_at"])} for r in oos]
    sources = {
        "opening_statistical_baseline": {r["ticker"]: r["baseline"] for r in oos},
        "baseline_expanding_up_rate": settled_audit.expanding_base_rate(as_audit, history),
        "baseline_persistence_last_settled": settled_audit.persistence(as_audit, history),
        "recorded_live_opening": {r["ticker"]: r["recorded_up"] for r in oos if type(r.get("recorded_up")) in (int, float)},
        "kalshi_market_mid_at_call": {r["ticker"]: r["quote"]["up_mid"] for r in oos if r.get("quote") and r["quote"].get("up_mid") is not None},
        "kalshi_preopen_mid_as_of_call": {r["ticker"]: r["kalshi_preopen_mid"] for r in oos if r.get("kalshi_preopen_mid") is not None},
        **predictions,
    }
    sealed = set(sealed_tickers)
    development = [r for r in oos if r["ticker"] not in sealed]
    holdout = [r for r in oos if r["ticker"] in sealed]
    if not development or not holdout:
        report["status"] = "INSUFFICIENT_OUT_OF_SAMPLE_CONTRACTS"
        report["reason"] = "Sealed holdout or development period has no out-of-sample predictions"
        return report
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
        base = [
            r for r in hold
            if r["ticker"] in by_ticker
            and r["ticker"] in sources["opening_statistical_baseline"]
        ]
        if name != "opening_statistical_baseline" and base:
            entry["holdout_log_loss_vs_baseline"] = rigor.paired_superiority_test(
                rigor._log_losses([by_ticker[r["ticker"]] for r in base], [r["outcome"] for r in base]),
                rigor._log_losses([r["baseline"] for r in base], [r["outcome"] for r in base]),
            )
        comparison[name] = entry
    report["model_comparison"] = comparison
    ablation = {}
    reference = predictions.get("compact_offset", {})
    for name in ("compact_plus_technicals", "compact_plus_flow", "compact_plus_candles",
                 "momentum_trend_volatility"):
        common = [r for r in oos if r["ticker"] in reference and r["ticker"] in predictions.get(name, {})]
        if common:
            ablation[name] = rigor.paired_superiority_test(
                rigor._log_losses([predictions[name][r["ticker"]] for r in common], [r["outcome"] for r in common]),
                rigor._log_losses([reference[r["ticker"]] for r in common], [r["outcome"] for r in common]),
            )
    family_of = {"compact_plus_technicals": "technicals_ema_rsi_macd_vwap_bollinger",
                 "compact_plus_flow": "order_book_flow_cvd", "compact_plus_candles": "candles",
                 "momentum_trend_volatility": "trend_ema_volume_acceleration"}
    for name, test in ablation.items():
        status = report["feature_family_history"].get(family_of.get(name), {}).get("status", "EVALUABLE")
        test["family_history_status"] = status
        test["interpretable"] = status == "EVALUABLE"
    report["incremental_value_vs_compact"] = ablation
    # Multiple testing: Holm step-down across every candidate compared with the
    # statistical baseline and, separately, with the existing bot's recorded
    # live openings on the same holdout contracts.
    live = sources.get("recorded_live_opening", {})
    vs_live = {}
    for name in MODELS:
        common = [r for r in holdout if r["ticker"] in predictions.get(name, {}) and r["ticker"] in live]
        if common:
            vs_live[name] = rigor.paired_superiority_test(
                rigor._log_losses([predictions[name][r["ticker"]] for r in common], [r["outcome"] for r in common]),
                rigor._log_losses([live[r["ticker"]] for r in common], [r["outcome"] for r in common]),
            )
    report["holdout_log_loss_vs_existing_bot"] = vs_live
    market = sources.get("kalshi_preopen_mid_as_of_call", {})
    vs_market = {}
    for name in MODELS:
        common = [r for r in holdout if r["ticker"] in predictions.get(name, {}) and r["ticker"] in market]
        if len(common) >= 2:
            vs_market[name] = rigor.paired_superiority_test(
                rigor._log_losses([predictions[name][r["ticker"]] for r in common], [r["outcome"] for r in common]),
                rigor._log_losses([market[r["ticker"]] for r in common], [r["outcome"] for r in common]),
            )
    report["holdout_log_loss_vs_kalshi_preopen_mid"] = vs_market
    if current_model:
        report["versus_running_model"] = current_model_comparison(holdout, predictions, current_model)
    # One family across every candidate x reference pair (plan section 4.4).
    family = {}
    for ref, tests in (("statistical_baseline", {n: e["holdout_log_loss_vs_baseline"] for n, e in comparison.items()
                                                 if n in MODELS and "holdout_log_loss_vs_baseline" in e}),
                       ("existing_bot", vs_live), ("kalshi_preopen_mid", vs_market)):
        for name, test in tests.items():
            family[f"{name} vs {ref}"] = test["p_value"]
    report["multiple_testing"] = {
        "method": "Holm step-down over every candidate x reference pair, family-wise alpha 0.05",
        "family_size": len(family),
        "results": holm(family),
    }
    report["quote_coverage"] = {
        "contracts_with_quote": sum(1 for r in rows if r.get("quote")),
        "from_opening_quote_snapshots": sum(1 for r in rows if (r.get("quote") or {}).get("source") == "opening_quote_snapshots"),
        "from_prediction_evidence": sum(1 for r in rows if (r.get("quote") or {}).get("source") == "predictions"),
        "fee_multiplier_assumed": sum(1 for r in rows if (r.get("quote") or {}).get("fee_multiplier_assumed")),
    }
    report["holdout_detectable_accuracy_margin"] = 1.96 * math.sqrt(0.25 / len(holdout)) if holdout else None
    report["status"] = "RESEARCH_COMPLETE"
    report["note"] = "Out-of-sample = walk-forward predictions on contracts unseen by training/calibration. Thresholds chosen on development only; holdout scored once. Not a profitability guarantee."
    return report


def feature_family_history(rows):
    """Plan section 5: a family is judged only where enough contracts carry it."""
    out = {}
    for group, names in FEATURE_GROUPS.items():
        carrying = [r for r in rows
                    if sum(r["features"].get(n) is not None for n in names) * 2 >= len(names)]
        out[group] = {
            "contracts_with_family": len(carrying),
            "first_available": carrying[0]["opens_at"] if carrying else None,
            "minimum_required": MIN_FAMILY_CONTRACTS,
            "status": "EVALUABLE" if len(carrying) >= MIN_FAMILY_CONTRACTS else "INSUFFICIENT_HISTORY",
        }
    return out


def day_block_bootstrap(priced, replicates=None, seed=None):
    from .settled_audit import BOOTSTRAP_REPLICATES, BOOTSTRAP_SEED, day_block_bootstrap as boot

    return boot(priced, replicates or BOOTSTRAP_REPLICATES, seed or BOOTSTRAP_SEED)


def holm(p_values, alpha=0.05):
    """Holm step-down adjusted p-values and rejections for a family of tests."""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    m, running, result = len(ordered), 0.0, {}
    for rank, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - rank) * p))
        result[name] = {"p_value": p, "holm_adjusted_p": running, "significant": running <= alpha}
    return result


def _candle_values(bars):
    """Past-only candle features from closed 1-minute bars (dicts), mirroring the
    opening snapshot's definitions."""
    from .prediction_engine import ema

    closes = [float(b["close"]) for b in bars]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    latest = bars[-1]
    o, high, low, close, volume = (float(latest[k]) for k in ("open", "high", "low", "close", "volume"))
    span = max(high - low, 1e-9)
    values = {f"return_{m}m": math.log(closes[-1] / closes[-m - 1]) for m in (1, 3, 5, 15, 30, 60) if len(closes) > m}
    prior_volume = sum(float(b["volume"]) for b in bars[-21:-1]) / 20
    ranges = [max(float(c["high"]) - float(c["low"]), abs(float(c["high"]) - float(p["close"])),
                  abs(float(c["low"]) - float(p["close"]))) for p, c in zip(bars, bars[1:])]
    window = ranges[-21:-1]
    mean_range = sum(window) / 20
    std = math.sqrt(sum((r - mean_range) ** 2 for r in window) / 20)
    atr = (ranges[-1] - mean_range) / std if std > 0 else None
    old_variance = sum(r * r for r in returns[-20:-5]) / 15
    settled = lambda period: ema(closes, period)[-1] if len(closes) >= 4 * period else None
    e9, e21, e50, e200 = (settled(p) for p in (9, 21, 50, 200))
    recent5 = sum(float(b["volume"]) for b in bars[-5:])
    prior5 = sum(float(b["volume"]) for b in bars[-10:-5])
    values.update(
        acceleration=returns[-1] - returns[-2],
        body_fraction=(close - o) / span, upper_wick=(high - max(close, o)) / span,
        lower_wick=(min(close, o) - low) / span, range_fraction=(high - low) / o,
        breakout_distance=(close - max(float(b["high"]) for b in bars[-21:-1])) / o,
        volatility_change=(sum(r * r for r in returns[-5:]) / 5) / old_variance if old_variance else None,
        relative_volume=volume / prior_volume if prior_volume else None,
        candle_body_range_ratio=abs(close - o) / span, candle_close_range_position=(close - low) / span,
        candle_volume_weighted_move=(close / o - 1) * (volume / prior_volume) if prior_volume else None,
        candle_atr_zscore_20=atr, candle_unusual_expansion=int(atr > 1.5) if atr is not None else None,
        opening_ema9_distance=close / e9 - 1 if e9 else None,
        opening_ema9_21_gap=e9 / e21 - 1 if e9 and e21 else None,
        opening_ema50_distance=close / e50 - 1 if e50 else None,
        opening_ema200_distance=close / e200 - 1 if e200 else None,
        opening_volume_acceleration=recent5 / prior5 - 1 if prior5 > 0 else None,
    )
    return values


def retrospective_rows(db, exclude_tickers, limit=RETROSPECTIVE_LIMIT, asof=None):
    """RETROSPECTIVE candle-only opening rows for settled contracts that have no
    eligible frozen opening record. Never mixed with the live record.

    Inputs are Coinbase 1-minute candles that closed AND were received (journal
    first_received_at) at or before the opening, plus the contract's strike and
    times as recorded live by the bot. Outcomes are official settlements only.
    The baseline is a proxy zero-drift Gaussian without the live opening-basis
    anchor, labelled accordingly. Nothing here is a live prediction.
    """
    from .prediction_engine import context24

    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"forecast_outcomes", "predictions", "btc_candles"} <= tables:
        return [], {"status": "UNAVAILABLE", "reason": "journal lacks outcomes, predictions or candles"}
    asof = asof or datetime.now(timezone.utc)
    outcomes = db.execute(
        """SELECT ticker,observed_at,outcome FROM forecast_outcomes WHERE origin='official'
             AND julianday(observed_at)<=julianday(?)
           ORDER BY observed_at DESC LIMIT ?""", (asof.isoformat(), limit * 2)
    ).fetchall()
    contracts, skipped = [], {}
    repeated = {t for t, n in _count(t for t, _o, _y in outcomes).items() if n > 1}
    for ticker, observed_at, outcome in outcomes:
        if ticker in repeated:
            skipped["REPEATED_OFFICIAL_OUTCOME"] = skipped.get("REPEATED_OFFICIAL_OUTCOME", 0) + 1
            continue
        if ticker in exclude_tickers or outcome not in (0, 1):
            continue
        meta = db.execute(
            """SELECT json_extract(payload,'$.market.opens_at'),json_extract(payload,'$.market.closes_at'),
                 json_extract(payload,'$.market.strike'),json_extract(payload,'$.market.mapping_verified'),
                 json_extract(payload,'$.market.comparison_operator'),
                 json_extract(payload,'$.market.averaging_rule_verified')
               FROM predictions WHERE ticker=? AND mode='PUBLIC MARKET DATA' ORDER BY timestamp LIMIT 1""",
            (ticker,),
        ).fetchone()
        if not meta or not meta[0] or not meta[1] or type(meta[2]) not in (int, float):
            skipped["NO_RECORDED_CONTRACT_METADATA"] = skipped.get("NO_RECORDED_CONTRACT_METADATA", 0) + 1
            continue
        if not meta[3] or meta[4] != ">=" or not meta[5]:
            skipped["UNVERIFIED_SETTLEMENT_RULES"] = skipped.get("UNVERIFIED_SETTLEMENT_RULES", 0) + 1
            continue
        contracts.append((ticker, timestamp(meta[0]), timestamp(meta[1]), float(meta[2]), observed_at, outcome))
        if len(contracts) >= limit:
            break
    if not contracts:
        return [], {"status": "NO_CONTRACTS", "skipped": skipped}
    first = int(min(c[1] for c in contracts).timestamp()) - 86520
    last = int(max(c[1] for c in contracts).timestamp())
    candles = {}
    for source, payload, received in db.execute(
        "SELECT source,payload,first_received_at FROM btc_candles WHERE start>=? AND start<=?", (first, last)
    ):
        if not str(source).startswith("Coinbase"):
            continue
        try:
            bar = json.loads(payload)
            start = int(bar["start"])
            stamp = timestamp(received)
        except (TypeError, ValueError, KeyError):
            continue
        prior = candles.get(start)
        if prior is None or stamp < prior[1]:
            candles[start] = (bar, stamp)
    rows = []
    # Sorted starts + bisect select exactly the bars the former full scan did:
    # cutoff - 86520 <= start <= cutoff - 60, received by the opening.
    starts = sorted(candles)
    for ticker, opened, closed, strike, observed_at, outcome in contracts:
        cutoff = opened.timestamp()
        low = bisect.bisect_left(starts, cutoff - 86520)
        high = bisect.bisect_right(starts, cutoff - 60)
        bars = [candles[start][0] for start in starts[low:high] if candles[start][1] <= opened]
        if not bars:
            skipped["NO_PRE_OPEN_CANDLES"] = skipped.get("NO_PRE_OPEN_CANDLES", 0) + 1
            continue
        received_at = max(candles[int(b["start"])][1] for b in bars)
        history = {"candles": bars, "received_at": received_at.isoformat(), "source": "Coinbase journal (retrospective)"}
        day = context24(history, opened)
        if day.get("status") != "READY":
            key = "INCOMPLETE_24H_PRE_OPEN_CANDLES"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        ordered = sorted((b for b in bars if day["first_start"] <= int(b["start"]) < day["last_close"]),
                         key=lambda b: int(b["start"]))
        values = {**day["features"], **_candle_values(ordered)}
        regime = str(day.get("regime") or "")
        values.update(regime_rising=1.0 if regime.startswith("rising") else 0.0,
                      regime_falling=1.0 if regime.startswith("falling") else 0.0)
        price = float(ordered[-1]["close"])
        deviation = day["sigma_dollars_per_sqrt_second"] * math.sqrt(max(60.0, (closed - opened).total_seconds() - 40))
        baseline = 0.5 * (1 + math.erf((price - strike) / (math.sqrt(2) * deviation)))
        rows.append(dict(
            ticker=ticker, timestamp=opened.isoformat(), opens_at=opened.isoformat(),
            closes_at=closed.isoformat(), label_available_at=observed_at, outcome=int(outcome),
            features={k: (float(v) if type(v) in (int, float) and math.isfinite(v) else None) for k, v in values.items()},
            baseline=min(1 - 1e-6, max(1e-6, baseline)), regime=day.get("regime"),
            recorded_up=None, recorded_model="RETROSPECTIVE_RECONSTRUCTION", quote=None,
        ))
    rows.sort(key=lambda r: (r["closes_at"], r["ticker"]))
    return rows, {
        "status": "RECONSTRUCTED",
        "contracts_considered": len(contracts),
        "reconstructed": len(rows),
        "skipped": skipped,
        "policy": "Candles closed and received by the opening; strike/times recorded live; official outcomes; proxy baseline without the live basis anchor. RETROSPECTIVE, not live performance.",
    }


def live_rows(db, asof=None):
    """Intracontract rows (separate from the immutable opening prediction).

    learning.dataset returns at most one stale-filtered live prediction per
    contract per time bucket, with candidate features and the executable quotes
    recorded at that same receipt time. The quote is the price a manual trader
    could have seen with that prediction; it is a cost input, never a feature.
    """
    from .scores import horizon

    tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "predictions" not in tables:
        return {}
    buckets = {}
    for row in learning.dataset(db, asof or datetime.now(timezone.utc)):
        quotes = row.get("executable_quotes") or {}
        up_ask = (quotes.get("up") or {}).get("ask")
        down_ask = (quotes.get("down") or {}).get("ask")
        multiplier = row.get("fee_multiplier")
        assumed = type(multiplier) not in (int, float) or not math.isfinite(multiplier)
        valid = all(type(v) in (int, float) and math.isfinite(v) and 0 < v < 1 for v in (up_ask, down_ask))
        row["quote"] = {
            "observed_at": row["timestamp"], "up_ask": up_ask, "down_ask": down_ask,
            "up_mid": row.get("market"), "source": "live_prediction_same_receipt",
            "fee_multiplier": ASSUMED_FEE_MULTIPLIER if assumed else multiplier,
            "fee_multiplier_assumed": assumed,
        } if valid else None
        buckets.setdefault(horizon(row["remaining"]), []).append(row)
    return buckets


def intracontract_research(buckets, progress=None, checkpoint=None):
    """Walk-forward research per time-remaining bucket; shadow only."""
    report = {}
    for bucket, rows in sorted(buckets.items()):
        if progress:
            progress(f"intracontract bucket {bucket}: {len(rows)} contracts")
        result = research(rows, progress=progress, checkpoint=checkpoint)
        result["scope"] = (
            f"Live intracontract predictions with {bucket} remaining; separate from the "
            "immutable opening prediction and never shown as an opening call"
        )
        report[bucket] = result
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
    versus = report.get("versus_running_model") or {}
    if versus.get("status") == "MEASURED":
        cur = versus["current_model"]
        lines.append(f"RUNNING MODEL holdout calls={cur['calls']} acc={cur['accuracy']} wilson95={cur['accuracy_wilson_95']} coverage={cur['coverage']:.3f}")
        for name, entry in versus["candidates"].items():
            same, matched = entry["same_contracts"], entry["matched_coverage"]
            lines.append(f"  {name:32s} same-contracts acc={same['accuracy']} McNemar p={same['paired_vs_current']['p_value']:.3f} "
                         f"| matched-coverage acc={matched['accuracy']} net/contract={matched['mean_net_pnl_per_contract']}")
    elif versus:
        lines.append(f"RUNNING MODEL comparison: {versus.get('status')} (openings by it in holdout: {versus.get('holdout_openings_by_current_model')})")
    for bucket, sub in (report.get("intracontract") or {}).items():
        lines.append(f"[intracontract {bucket}] {sub.get('status')} contracts={sub.get('eligible_opening_contracts')}")
        for name, entry in (sub.get("model_comparison") or {}).items():
            chosen = entry.get("holdout_at_chosen_threshold") or {}
            lines.append(
                f"  {name:32s} logloss={entry['holdout_probabilistic']['log_loss']:.4f} "
                f"threshold={entry.get('threshold_chosen_on_development')} calls={chosen.get('calls')} "
                f"acc={chosen.get('accuracy')} net/contract={chosen.get('mean_net_pnl_per_contract')}"
            )
    return "\n".join(lines)


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("opening_research.report.json"))
    parser.add_argument("--intracontract", action="store_true",
                        help="Also evaluate live intracontract predictions per time-remaining bucket")
    parser.add_argument("--retrospective", action="store_true",
                        help="Also evaluate candle-only RETROSPECTIVE openings for settled contracts without frozen records")
    parser.add_argument("--audit-only", action="store_true",
                        help="Write only Report A (the settled audit of displayed calls)")
    parser.add_argument("--asof", help="ISO-8601 cutoff; only labels observed by then are used. "
                        "Pin it to resume a run on exactly the same data.")
    parser.add_argument("--current-model",
                        help="Recorded model label of the running model, e.g. opening-candidate/<model_id>; "
                             "candidates are compared with it on the same holdout contracts")
    parser.add_argument("--checkpoint-dir", type=Path,
                        help="Research-output directory for resumable fold checkpoints")
    args = parser.parse_args()
    started = time.monotonic()
    phase_times = {}

    def progress(message):
        print(f"RESEARCH_PROGRESS {_stamp()} +{time.monotonic() - started:.0f}s {message}", flush=True)

    def phase(name, since):
        phase_times[name] = round(time.monotonic() - since, 1)
        progress(f"PHASE {name} finished in {phase_times[name]}s")

    try:
        asof = timestamp(args.asof) if args.asof else datetime.now(timezone.utc)
        if asof.tzinfo is None:
            raise ValueError("--asof must include a timezone")
        checkpoint = fast.Checkpoints(args.checkpoint_dir) if args.checkpoint_dir else None
        rigor._refuse_foreign_file(args.output, REPORT_FORMAT)
        from .model_registry import atomic_json

        progress(f"as-of {asof.isoformat()}; numpy solver={'yes' if fast.numpy_available() else 'NO (pure Python fallback)'}; "
                 f"checkpoints={args.checkpoint_dir or 'disabled'}")
        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            t = time.monotonic()
            coverage = opening_models.dataset_coverage(db, asof)
            # Report A first: the verified historical audit of displayed calls,
            # written and printed before any experimental model research runs.
            audit = settled_audit.audit(db, asof)
            audit["authoritative_dataset_coverage"] = coverage
            audit_path = args.output.with_name("report_A_settled_audit.json")
            rigor._refuse_foreign_file(audit_path, settled_audit.AUDIT_FORMAT)
            atomic_json(audit_path, audit)
            print("REPORT_A_BEGIN", flush=True)
            for line in settled_audit.summary_lines(audit):
                print(line, flush=True)
            print("REPORT_A_END", flush=True)
            phase("report_A", t)
            if args.audit_only:
                return

            # Core Report B: frozen openings only. Written before any optional
            # section is even loaded, so a later interruption cannot lose it.
            t = time.monotonic()
            rows = load(db, asof)
            phase("load_frozen_openings", t)
            print(f"Loaded {len(rows)} eligible frozen openings (read-only)", flush=True)
            t = time.monotonic()
            report = research(rows, progress=progress, checkpoint=checkpoint, current_model=args.current_model)
            report.update(asof=asof.isoformat(), authoritative_dataset_coverage=coverage,
                          optional_section_errors={}, optional_sections_pending=[
                              name for name, wanted in (("retrospective", args.retrospective),
                                                        ("intracontract", args.intracontract)) if wanted],
                          phase_seconds=phase_times)
            phase("report_B_core", t)
            atomic_json(args.output, report)
            print("REPORT_B_BEGIN", flush=True)
            print(summary_text(report), flush=True)
            print("REPORT_B_END", flush=True)
            exclude = {r["ticker"] for r in rows}
            del rows

            if args.retrospective:
                t = time.monotonic()
                try:
                    retro, retro_info = retrospective_rows(db, exclude, asof=asof)
                    has_snapshots = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opening_quote_snapshots'"
                    ).fetchone()
                    for row in retro:  # executable quotes after the opening, for costs only
                        row["quote"] = (_snapshot_quote_at(db, row) if has_snapshots else None) or _quote_at(db, row)
                    phase("load_retrospective", t)
                    print(f"Retrospective candle-only reconstruction: {retro_info}", flush=True)
                    t = time.monotonic()
                    report["retrospective_candle_only"] = {
                        "reconstruction": retro_info,
                        **research(retro, progress=progress, checkpoint=checkpoint)}
                    phase("research_retrospective", t)
                    del retro
                except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                    report["optional_section_errors"]["retrospective"] = type(exc).__name__ + ": " + str(exc)[:200]
                report["optional_sections_pending"].remove("retrospective")
                atomic_json(args.output, report)

            if args.intracontract:
                t = time.monotonic()
                try:
                    buckets = live_rows(db, asof)
                    phase("load_intracontract", t)
                    print("Loaded intracontract rows: " + ", ".join(
                        f"{k}={len(v)}" for k, v in sorted(buckets.items())), flush=True)
                    t = time.monotonic()
                    report["intracontract"] = intracontract_research(buckets, progress=progress, checkpoint=checkpoint)
                    phase("research_intracontract", t)
                    del buckets
                except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                    report["optional_section_errors"]["intracontract"] = type(exc).__name__ + ": " + str(exc)[:200]
                report["optional_sections_pending"].remove("intracontract")
                atomic_json(args.output, report)
        if report["optional_section_errors"]:
            print(f"Optional research sections unavailable: {report['optional_section_errors']}", flush=True)
        if checkpoint:
            report["checkpoints"] = {"reused_fold_models": checkpoint.hits, "computed_fold_models": checkpoint.misses}
            atomic_json(args.output, report)
        print("REPORT_B_OPTIONAL_BEGIN", flush=True)
        print(summary_text({**report, "model_comparison": {}}), flush=True)
        print("REPORT_B_OPTIONAL_END", flush=True)
        progress(f"ALL PHASES COMPLETE {phase_times}")
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

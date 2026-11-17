"""Read-only settled-market audit of the opening calls actually displayed. RESEARCH ONLY.

Phase 3 of docs/RESEARCH_PLAN_SETTLED_ACCURACY.md. Unlike the training loader
(``opening_models.dataset``), which keeps only openings whose frozen feature
vector matches the current feature version, this audit scores EVERY immutable
opening that was recorded and shown, whatever model or feature version made it,
because the question is "how accurate were the calls the user saw".

The journal is opened read-only by the caller. Nothing here writes to a
journal, artifact, registry or service, places an order, or changes a model,
threshold or displayed confidence. Probabilities are scored exactly as
recorded; nothing is recomputed with later data.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import random

from . import rigorous_evaluation as rigor
from .calibration import fit_platt
from .model import timestamp
from .scores import score, wilson_interval

AUDIT_FORMAT = "kxbtc-settled-opening-audit-v1"
MODE = "PUBLIC MARKET DATA"
CONTRACT_SECONDS = 900
SETTLEMENT_GRACE_SECONDS = 3600  # an opening closed >1 h ago should be settled
SEALED_HOLDOUT_FRACTION = 0.20
PREOPEN_QUOTE_WINDOW_SECONDS = 60
CONFIDENCE_BANDS = ((0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
                    (0.70, 0.80), (0.80, 0.90), (0.90, 1.0000001))
DISPLAYABLE_CONFIDENCE = {"CALIBRATED", "EXPERIMENTAL CALIBRATED CANDIDATE"}
HIGH_CONFIDENCE_CLAIM = {"min_confidence": 0.80, "min_calls": 100, "min_wilson_lower": 0.80}
PHASE4_FIXED_THRESHOLD = 0.55
PHASE4_THRESHOLD_CHALLENGERS = (0.50, 0.52, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80)
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261117


def _tables(db):
    return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _finite_probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def preopen_mid(db, ticker, opened):
    """Latest two-sided Kalshi UP mid observed in [open-60s, open]. Never later."""
    found = db.execute(
        """SELECT up_bid,up_ask FROM opening_quote_snapshots
           WHERE ticker=? AND mode=? AND julianday(observed_at)>=julianday(?)
             AND julianday(observed_at)<=julianday(?)
             AND up_bid IS NOT NULL AND up_ask IS NOT NULL
           ORDER BY observed_at DESC LIMIT 1""",
        (ticker, MODE, (opened - timedelta(seconds=PREOPEN_QUOTE_WINDOW_SECONDS)).isoformat(),
         opened.isoformat()),
    ).fetchone()
    if not found:
        return None
    bid, ask = found
    if not (_finite_probability(bid) and _finite_probability(ask) and 0 < bid <= ask < 1):
        return None
    return (bid + ask) / 2


def _receipt_order(observed):
    try:
        return (0, timestamp(observed).timestamp(), "")
    except (TypeError, ValueError, AttributeError, OverflowError):
        return (1, 0.0, str(observed))


def official_outcomes(db):
    """One trusted official label per ticker, plus the quarantined tickers.

    (Codex Phase 4) Conflicting or invalid official rows are quarantined
    instead of trusting whichever row SQLite returns last. Identical repeats
    keep the EARLIEST receipt so chronology is never improved by a later
    copy. Every consumer (scoring, baselines) uses this single reader.
    """
    outcomes, quarantined, evidence = {}, set(), {}
    if "forecast_outcomes" not in _tables(db):
        return outcomes, quarantined
    for ticker, observed, outcome in db.execute(
        "SELECT ticker,observed_at,outcome FROM forecast_outcomes WHERE origin='official'"
    ):
        evidence.setdefault(ticker, []).append((observed, outcome))
    for ticker, items in evidence.items():
        valid = {(observed, outcome) for observed, outcome in items if outcome in (0, 1)}
        has_invalid = any(outcome not in (0, 1) for _observed, outcome in items)
        if has_invalid or len({outcome for _observed, outcome in valid}) != 1:
            quarantined.add(ticker)
            continue
        outcomes[ticker] = min(valid, key=lambda item: _receipt_order(item[0]))
    return outcomes, quarantined


def official_history(db, asof):
    """(receipt time, outcome) of trusted official labels known by ``asof``;
    the baselines use this, so quarantined settlements never enter them."""
    history = []
    for observed, outcome in official_outcomes(db)[0].values():
        try:
            t = timestamp(observed)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if t <= asof:
            history.append((t, outcome))
    return sorted(history)


def load_openings(db, asof):
    """Every recorded opening with its official outcome (or why it has none)."""
    tables = _tables(db)
    if "btc_engine_opening" not in tables:
        return [], {"btc_engine_opening": "TABLE_MISSING"}
    outcomes, quarantined = official_outcomes(db)
    problems = {}
    if quarantined:
        problems["CONFLICTING_OR_INVALID_OFFICIAL_OUTCOME"] = len(quarantined)
    has_quotes = "opening_quote_snapshots" in tables
    rows = []
    for ticker, generated, cutoff, closes, up, payload in db.execute(
        "SELECT ticker,generated_at,cutoff,closes_at,up,payload FROM btc_engine_opening WHERE mode=?",
        (MODE,),
    ):
        try:
            saved = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            saved = {}
            problems["UNPARSEABLE_PAYLOAD"] = problems.get("UNPARSEABLE_PAYLOAD", 0) + 1
        try:
            opened = timestamp(saved.get("opens_at") or cutoff)
            closed = timestamp(closes)
        except (TypeError, ValueError, AttributeError, OverflowError):
            problems["UNPARSEABLE_TIMES"] = problems.get("UNPARSEABLE_TIMES", 0) + 1
            continue
        observed, outcome = outcomes.get(ticker, (None, None))
        try:
            label_at = timestamp(observed) if observed else None
        except (TypeError, ValueError, AttributeError, OverflowError):
            label_at, outcome = None, None
        if label_at is not None and label_at > asof:
            label_at, outcome = None, None  # not yet known as of the audit time
        frozen = saved.get("frozen_features") or {}
        quality = saved.get("data_quality") or {}
        rows.append({
            "ticker": ticker,
            "opens_at": opened,
            "closes_at": closed,
            "generated_at": generated,
            "up": up if _finite_probability(up) else None,
            "direction": saved.get("direction"),
            "confidence_status": (saved.get("confidence") or {}).get("status"),
            "threshold": (saved.get("decision_policy") or {}).get("threshold"),
            "model": saved.get("model") or "UNRECORDED",
            "regime": saved.get("regime") or "unavailable",
            "settlement_quarantined": ticker in quarantined,
            "feature_version": frozen.get("version"),
            "max_input_received_at": frozen.get("max_input_received_at"),
            "history_last_close": saved.get("history_last_close"),
            "complete_history": quality.get("closed_history_bars") == 1440,
            "cutoff_is_open": str(saved.get("data_cutoff") or "") != "" and _same_time(saved.get("data_cutoff"), opened),
            "outcome": outcome if outcome in (0, 1) else None,
            "label_available_at": label_at,
            "kalshi_preopen_mid": preopen_mid(db, ticker, opened) if has_quotes else None,
        })
    rows.sort(key=lambda r: (r["opens_at"], r["ticker"]))
    return rows, problems


def _same_time(value, when):
    try:
        return timestamp(value) == when
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False


def displayed(row):
    """The live terminal shows a call only for UP/DOWN with displayable confidence."""
    return (
        row["direction"] in {"UP", "DOWN"}
        and row["confidence_status"] in DISPLAYABLE_CONFIDENCE
        and row["up"] is not None
        and (row["direction"] == "UP") == (row["up"] >= 0.5)
    )


# ------------------------------------------------------------------- audit


def quote_snapshot_coverage(db, rows):
    """Why a market-price comparison may be impossible: is there any pre-open
    Kalshi quote at all, and when did recording start?"""
    if "opening_quote_snapshots" not in _tables(db):
        return {"table_present": False,
                "reading": "This journal predates pre-open Kalshi quote capture; no market comparison is possible from it."}
    total, tickers, first, last, preopen, preopen_two_sided, modes = db.execute(
        """SELECT COUNT(*),COUNT(DISTINCT ticker),MIN(observed_at),MAX(observed_at),
                  SUM(seconds_from_open<=0),
                  SUM(seconds_from_open<=0 AND up_bid IS NOT NULL AND up_ask IS NOT NULL),
                  GROUP_CONCAT(DISTINCT mode)
           FROM opening_quote_snapshots"""
    ).fetchone()
    settled = {r["ticker"] for r in rows if r["outcome"] is not None}
    settled_with_preopen = 0
    if settled:
        for (ticker,) in db.execute(
            "SELECT DISTINCT ticker FROM opening_quote_snapshots WHERE seconds_from_open<=0 AND mode=?", (MODE,)
        ):
            settled_with_preopen += ticker in settled
    first_opening = rows[0]["opens_at"].isoformat() if rows else None
    return {
        "table_present": True, "rows": total, "distinct_tickers": tickers,
        "first_observed_at": first, "last_observed_at": last,
        "rows_at_or_before_open": preopen or 0,
        "two_sided_rows_at_or_before_open": preopen_two_sided or 0,
        "modes": modes, "settled_openings_with_preopen_quote": settled_with_preopen,
        "first_recorded_opening": first_opening,
        "reading": (
            "No quote was recorded at or before any opening; only post-open quotes exist."
            if total and not preopen else
            "Quote capture began after the settled history; coverage grows from first_observed_at."
            if total else "Table exists but is empty."
        ),
    }


def daily_breakdown(rows):
    """Per-UTC-day counts that show when displayed calls stopped or started."""
    days = {}
    for r in rows:
        day = days.setdefault(r["opens_at"].date().isoformat(), {
            "openings": 0, "settled": 0, "displayed_calls": 0, "correct": 0,
            "models": {}, "confidence_status": {}, "directions": {}})
        day["openings"] += 1
        day["settled"] += r["outcome"] is not None
        if r["outcome"] is not None and displayed(r):
            day["displayed_calls"] += 1
            day["correct"] += (r["direction"] == "UP") == bool(r["outcome"])
        for key, value in (("models", r["model"]), ("confidence_status", str(r["confidence_status"])),
                           ("directions", str(r["direction"]))):
            day[key][value] = day[key].get(value, 0) + 1
    return dict(sorted(days.items()))


def no_call_reasons(rows):
    """Why settled openings were not displayed calls, one reason each."""
    reasons = {}
    for r in rows:
        if displayed(r):
            continue
        if r["up"] is None:
            reason = "NO_PROBABILITY_RECORDED"
        elif r["direction"] not in {"UP", "DOWN"}:
            reason = f"DIRECTION_{r['direction']}"
        elif r["confidence_status"] not in DISPLAYABLE_CONFIDENCE:
            reason = f"CONFIDENCE_{r['confidence_status']}"
        else:
            reason = "DIRECTION_CONTRADICTS_PROBABILITY"
        reasons[reason] = reasons.get(reason, 0) + 1
    confidences = sorted(max(r["up"], 1 - r["up"]) for r in rows if r["up"] is not None)
    return {
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "max_confidence": confidences[-1] if confidences else None,
        "median_confidence": confidences[len(confidences) // 2] if confidences else None,
        "thresholds_seen": _count(str(r["threshold"]) for r in rows),
        "models_seen": _count(r["model"] for r in rows),
    }


def model_versions(settled):
    """Per recorded model: never mix versions when judging a model."""
    out = {}
    for model in sorted({r["model"] for r in settled}):
        group = [r for r in settled if r["model"] == model]
        calls = [r for r in group if displayed(r)]
        scored = [r for r in group if r["up"] is not None]
        s = score([r["up"] for r in scored], [r["outcome"] for r in scored]) if scored else {}
        out[model] = {
            "first_opening": group[0]["opens_at"].isoformat(),
            "last_opening": group[-1]["opens_at"].isoformat(),
            "settled_openings": len(group),
            "displayed_calls": len(calls),
            **{k: v for k, v in _calls(calls).items() if k != "calls"},
            "brier_all_recorded": s.get("brier"),
            "log_loss_all_recorded": s.get("log_loss"),
            "ece_all_recorded": s.get("expected_calibration_error"),
            "thresholds_seen": _count(str(r["threshold"]) for r in group),
        }
    return out


def dashboard_scoreboard(rows):
    """Exact replica of Journal.verified_opening_scoreboard's counting rules
    (displayed UP/DOWN, calibrated status, consistent probability, official
    settlement), so the dashboard counter can be reconciled record by record."""
    calls = [r for r in rows if r["outcome"] is not None and displayed(r)]
    out = {**_calls(calls),
           "first_call_opens_at": calls[0]["opens_at"].isoformat() if calls else None,
           "last_call_opens_at": calls[-1]["opens_at"].isoformat() if calls else None,
           "by_model": {m: _calls([r for r in calls if r["model"] == m])
                        for m in sorted({r["model"] for r in calls})},
           "single_model": len({r["model"] for r in calls}) <= 1,
           # The live counter takes whichever duplicate official row SQLite
           # returns first; quarantined settlements are excluded here instead.
           "displayed_calls_excluded_for_conflicting_settlement": sum(
               r.get("settlement_quarantined", False) and displayed(r) for r in rows)}
    for window in (26, 50, 100):
        recent = calls[-window:]
        if recent:
            out[f"most_recent_{window}"] = {
                **_calls(recent),
                "from": recent[0]["opens_at"].isoformat(),
                "models": _count(r["model"] for r in recent),
            }
    return out


def integrity_checks(db, rows, asof):
    """Adversarial re-checks of the audit's own inputs. Any non-zero count is a
    reason to distrust the affected rows; none are silently repaired."""
    tables = _tables(db)
    leak, late_generation, early_generation, bad_duration = [], [], [], []
    early_label, history_after_open = [], []
    for r in rows:
        try:
            if r["max_input_received_at"] and timestamp(r["max_input_received_at"]) > r["opens_at"]:
                leak.append(r["ticker"])
        except (TypeError, ValueError, AttributeError, OverflowError):
            leak.append(r["ticker"])
        try:
            generated = timestamp(r["generated_at"])
            if generated >= r["closes_at"]:
                late_generation.append(r["ticker"])
            elif generated < r["opens_at"] - timedelta(seconds=5):
                early_generation.append(r["ticker"])
        except (TypeError, ValueError, AttributeError, OverflowError):
            late_generation.append(r["ticker"])
        if not 60 <= (r["closes_at"] - r["opens_at"]).total_seconds() <= CONTRACT_SECONDS:
            bad_duration.append(r["ticker"])
        if r["label_available_at"] is not None and r["label_available_at"] < r["closes_at"]:
            early_label.append(r["ticker"])
        try:
            if r["history_last_close"] is not None and float(r["history_last_close"]) > r["opens_at"].timestamp():
                history_after_open.append(r["ticker"])
        except (TypeError, ValueError, OverflowError):
            pass
    by_open = {}
    for r in rows:
        by_open.setdefault(r["opens_at"], []).append(r["ticker"])
    same_open = {k.isoformat(): v for k, v in by_open.items() if len(v) > 1}
    duplicate_tickers = len(rows) - len({r["ticker"] for r in rows})
    disagreements = 0
    duplicate_official_rows = 0
    conflicting_official_tickers = 0
    invalid_official_rows = 0
    origins = {}
    if "forecast_outcomes" in tables:
        labels = {}
        official_by_ticker = {}
        for ticker, origin, outcome in db.execute("SELECT ticker,origin,outcome FROM forecast_outcomes"):
            origins[origin] = origins.get(origin, 0) + 1
            labels.setdefault(ticker, {})[origin] = outcome
            if origin == "official":
                official_by_ticker.setdefault(ticker, []).append(outcome)
        duplicate_official_rows = sum(max(0, len(values) - 1) for values in official_by_ticker.values())
        invalid_official_rows = sum(value not in (0, 1) for values in official_by_ticker.values() for value in values)
        conflicting_official_tickers = sum(
            any(value not in (0, 1) for value in values)
            or len({value for value in values if value in (0, 1)}) != 1
            for values in official_by_ticker.values()
        )
        disagreements = sum(
            1 for v in labels.values()
            if "official" in v and any(o != v["official"] for k, o in v.items() if k != "official" and o in (0, 1))
        )
    settled = [r for r in rows if r["outcome"] is not None]
    n = len(settled)
    calls = sum(displayed(r) for r in settled)
    return {
        "post_open_input_receipt": {"count": len(leak), "examples": leak[:10]},
        "history_ends_after_open": {"count": len(history_after_open), "examples": history_after_open[:10]},
        "generated_at_or_after_close": {"count": len(late_generation), "examples": late_generation[:10]},
        "generated_more_than_5s_before_open": {"count": len(early_generation), "examples": early_generation[:10]},
        "contract_duration_outside_1_to_15_min": {"count": len(bad_duration), "examples": bad_duration[:10]},
        "settlement_observed_before_close": {"count": len(early_label), "examples": early_label[:10]},
        "duplicate_ticker_rows": duplicate_tickers,
        "multiple_tickers_same_open": {"count": len(same_open), "examples": dict(list(same_open.items())[:5])},
        "outcome_origins": origins,
        "duplicate_official_outcome_rows": duplicate_official_rows,
        "invalid_official_outcome_rows": invalid_official_rows,
        "conflicting_or_invalid_official_outcome_tickers": conflicting_official_tickers,
        "official_vs_other_origin_disagreements": disagreements,
        "sample_size": {
            "settled": n, "displayed_calls": calls,
            "accuracy_95_half_width_at_50pct": 1.96 * math.sqrt(0.25 / calls) if calls else None,
            "calls_needed_to_detect_55pct_vs_50pct": 783,
            "calls_needed_to_detect_60pct_vs_50pct": 194,
            "note": "Two-sided alpha 0.05, power 0.80, independent contracts; serial correlation raises these.",
        },
    }


def journal_audit(db, rows, problems, asof):
    """Counts of verified settled markets and every missing/incomplete record."""
    tables = _tables(db)
    official = 0
    if "forecast_outcomes" in tables:
        official = db.execute(
            "SELECT COUNT(*) FROM forecast_outcomes WHERE origin='official'"
        ).fetchone()[0]
    opening_tickers = {r["ticker"] for r in rows}
    settled_without_opening = 0
    if "forecast_outcomes" in tables:
        for (ticker,) in db.execute("SELECT ticker FROM forecast_outcomes WHERE origin='official'"):
            if ticker not in opening_tickers:
                settled_without_opening += 1
    grace = timedelta(seconds=SETTLEMENT_GRACE_SECONDS)
    settled = [r for r in rows if r["outcome"] is not None]
    awaiting = [r for r in rows if r["outcome"] is None and r["closes_at"] + grace > asof]
    missing_settlement = [r for r in rows if r["outcome"] is None and r["closes_at"] + grace <= asof]
    # Contract cadence gaps: 15-minute slots between the first and last
    # recorded opening that have no opening record at all.
    gaps, gap_ranges = 0, []
    starts = sorted({int(r["opens_at"].timestamp()) // CONTRACT_SECONDS for r in rows})
    for a, b in zip(starts, starts[1:]):
        if b - a > 1:
            gaps += b - a - 1
            gap_ranges.append((b - a - 1, a + 1, b - 1))
    gap_ranges.sort(reverse=True)
    incomplete = {
        "probability_missing": sum(r["up"] is None for r in rows),
        "direction_missing": sum(r["direction"] is None for r in rows),
        "confidence_status_missing": sum(r["confidence_status"] is None for r in rows),
        "history_not_1440_bars": sum(not r["complete_history"] for r in rows),
        "data_cutoff_not_equal_open": sum(not r["cutoff_is_open"] for r in rows),
        "direction_contradicts_probability": sum(
            r["direction"] in {"UP", "DOWN"} and r["up"] is not None
            and (r["direction"] == "UP") != (r["up"] >= 0.5) for r in rows
        ),
        "no_preopen_kalshi_mid": sum(r["kalshi_preopen_mid"] is None for r in rows),
    }
    return {
        "tables_present": sorted(tables),
        "recorded_openings": len(rows),
        "first_opening": rows[0]["opens_at"].isoformat() if rows else None,
        "last_opening": rows[-1]["opens_at"].isoformat() if rows else None,
        "official_settlements_in_journal": official,
        "verified_settled_openings": len(settled),
        "awaiting_settlement": len(awaiting),
        "missing_settlement_after_grace": len(missing_settlement),
        "missing_settlement_examples": [r["ticker"] for r in missing_settlement[-10:]],
        "settled_contracts_without_opening_record": settled_without_opening,
        "missing_15_minute_slots_between_first_and_last": gaps,
        "largest_slot_gaps": [
            {"missing_contracts": n,
             "from": datetime.fromtimestamp(a * CONTRACT_SECONDS, timezone.utc).isoformat(),
             "to": datetime.fromtimestamp(b * CONTRACT_SECONDS, timezone.utc).isoformat()}
            for n, a, b in gap_ranges[:10]
        ],
        "incomplete_records": incomplete,
        "unparseable": problems,
        "by_model": _count(r["model"] for r in rows),
        "by_feature_version": _count(str(r["feature_version"]) for r in rows),
        "by_confidence_status": _count(str(r["confidence_status"]) for r in rows),
        "by_saved_direction": _count(str(r["direction"]) for r in rows),
    }


# --------------------------------------------------------------- measurement


def _calls(rows):
    n = len(rows)
    correct = sum((r["direction"] == "UP") == bool(r["outcome"]) for r in rows)
    return {"calls": n, "correct": correct, "wrong": n - correct,
            "accuracy": correct / n if n else None,
            "accuracy_wilson_95": wilson_interval(correct, n)}


def displayed_accuracy(settled):
    calls = [r for r in settled if displayed(r)]
    out = {
        "scope": "Immutable opening calls the terminal displayed (UP/DOWN with calibrated status), settled officially",
        "settled_openings": len(settled),
        "displayed_calls": len(calls),
        "withheld_or_wait": len(settled) - len(calls),
        "coverage": len(calls) / len(settled) if settled else None,
        **{k: v for k, v in _calls(calls).items() if k != "calls"},
        "up_calls": _calls([r for r in calls if r["direction"] == "UP"]),
        "down_calls": _calls([r for r in calls if r["direction"] == "DOWN"]),
        "settled_up_rate": (sum(r["outcome"] for r in settled) / len(settled)) if settled else None,
        "by_model": {m: _calls([r for r in calls if r["model"] == m])
                     for m in sorted({r["model"] for r in calls})},
        "by_confidence_band": confidence_bands(calls),
    }
    claim = HIGH_CONFIDENCE_CLAIM
    high = [r for r in calls if max(r["up"], 1 - r["up"]) >= claim["min_confidence"]]
    stats = _calls(high)
    lower = stats["accuracy_wilson_95"][0] if high and stats["accuracy_wilson_95"] else None
    out["high_confidence_claim"] = {
        **stats, "rule": claim,
        "claim_supported": bool(high and stats["calls"] >= claim["min_calls"]
                                and lower is not None and lower >= claim["min_wilson_lower"]),
    }
    return out


def _threshold_metrics(rows, threshold):
    """Counterfactual symmetric threshold over immutable probabilities.

    This never rewrites the historical displayed direction.  It answers the
    narrower research question: what would the *same frozen probabilities*
    have done under a predeclared abstention cutoff?
    """
    eligible = [r for r in rows if r["up"] is not None]
    called = [r for r in eligible if max(r["up"], 1 - r["up"]) >= threshold]
    correct = sum((r["up"] >= 0.5) == bool(r["outcome"]) for r in called)
    probabilities = [r["up"] for r in called]
    outcomes = [r["outcome"] for r in called]
    probabilistic = score(probabilities, outcomes) if called else None
    return {
        "threshold": threshold,
        "eligible_probabilities": len(eligible),
        "calls": len(called),
        "wait": len(eligible) - len(called),
        "coverage": len(called) / len(eligible) if eligible else None,
        "correct": correct,
        "wrong": len(called) - correct,
        "accuracy": correct / len(called) if called else None,
        "accuracy_wilson_95": wilson_interval(correct, len(called)),
        "brier_on_called": probabilistic["brier"] if probabilistic else None,
        "log_loss_on_called": probabilistic["log_loss"] if probabilistic else None,
        "ece_on_called": probabilistic["expected_calibration_error"] if probabilistic else None,
    }


def threshold_challenge(settled):
    """Pooled challenge (reference) plus one per recorded model version."""
    pooled = _threshold_challenge(settled)
    models = sorted({r["model"] for r in settled})
    pooled["model_scope"] = "SINGLE_MODEL" if len(models) <= 1 else "MIXED_MODEL_VERSIONS_REFERENCE_ONLY"
    pooled["by_model"] = {m: _threshold_challenge([r for r in settled if r["model"] == m]) for m in models}
    return pooled


def _threshold_challenge(settled):
    """Challenge 0.55 without tuning on the chronological sealed holdout.

    Alternative thresholds and regime slices are development-only exploratory
    diagnostics.  The holdout is opened only for the already-declared 0.55
    policy, preventing this report from choosing a threshold on final data.
    """
    development, holdout, seal = sealed_split(settled)
    alternatives = {
        f"{threshold:.2f}": _threshold_metrics(development, threshold)
        for threshold in PHASE4_THRESHOLD_CHALLENGERS
    }
    regimes = {}
    for regime in sorted({str(r.get("regime") or "unavailable") for r in development}):
        group = [r for r in development if str(r.get("regime") or "unavailable") == regime]
        regimes[regime] = _threshold_metrics(group, PHASE4_FIXED_THRESHOLD)
    return {
        "role": "COUNTERFACTUAL_SHADOW_RESEARCH_ONLY",
        "historical_displayed_calls_unchanged": True,
        "fixed_policy": PHASE4_FIXED_THRESHOLD,
        "sealed_split": seal,
        "development_alternatives": alternatives,
        "fixed_policy_on_sealed_holdout": _threshold_metrics(holdout, PHASE4_FIXED_THRESHOLD),
        "development_regime_slices_at_fixed_policy": regimes,
        "selection_allowed": False,
        "note": (
            "Only the predeclared 0.55 threshold is evaluated on the sealed holdout. "
            "Alternative thresholds and regime slices are exploratory development evidence, "
            "not authorization to change production."
        ),
    }


def confidence_bands(rows):
    table = {}
    for low, high in CONFIDENCE_BANDS:
        group = [r for r in rows if low <= max(r["up"], 1 - r["up"]) < high]
        stats = _calls(group)
        stats["mean_confidence"] = (sum(max(r["up"], 1 - r["up"]) for r in group) / len(group)
                                    if group else None)
        table[f"{low:.2f}-{min(high, 1.0):.2f}"] = stats
    return table


def calibration(settled):
    """Calibration of every recorded probability, displayed or not."""
    scored = [r for r in settled if r["up"] is not None]
    if not scored:
        return {"samples": 0, "status": "INSUFFICIENT_DATA"}
    probabilities = [r["up"] for r in scored]
    outcomes = [r["outcome"] for r in scored]
    result = score(probabilities, outcomes)
    try:
        platt = fit_platt(probabilities, outcomes, regularization=1e-6)
        slope = {"slope": platt["slope"], "intercept": platt["intercept"],
                 "reading": "slope 1, intercept 0 = calibrated; slope < 1 = over-confident; slope > 1 = under-confident. Diagnostic only, never applied."}
    except (ValueError, ZeroDivisionError, OverflowError):
        slope = None
    by_status = {}
    for status in sorted({str(r["confidence_status"]) for r in scored}):
        group = [r for r in scored if str(r["confidence_status"]) == status]
        s = score([r["up"] for r in group], [r["outcome"] for r in group])
        by_status[status] = {k: s[k] for k in ("samples", "brier", "log_loss", "expected_calibration_error")}
    return {
        "samples": len(scored),
        "brier": result["brier"],
        "brier_approximate_interval": result["brier_approximate_interval"],
        "log_loss": result["log_loss"],
        "expected_calibration_error": result["expected_calibration_error"],
        "reliability_bins": [b for b in result["calibration"] if b["samples"]],
        "calibration_slope": slope,
        "by_confidence_status": by_status,
        "probability_spread": {
            "min": min(probabilities), "max": max(probabilities),
            "share_within_0.45_0.55": sum(0.45 <= p <= 0.55 for p in probabilities) / len(probabilities),
        },
    }


def expanding_base_rate(rows, history):
    """B0: P(UP) from official outcomes whose label was available before each opening."""
    history = sorted((t, y) for t, y in history if t is not None and y in (0, 1))
    out, ups, n, i = {}, 0, 0, 0
    for row in rows:
        while i < len(history) and history[i][0] <= row["opens_at"]:
            ups += history[i][1]
            n += 1
            i += 1
        out[row["ticker"]] = (ups + 1) / (n + 2)
    return out


def persistence(rows, history):
    """B4: last available settled direction, with a learned (past-only) repeat rate."""
    history = sorted((t, y) for t, y in history if t is not None and y in (0, 1))
    out, i, last, repeats, pairs = {}, 0, None, 0, 0
    for row in rows:
        while i < len(history) and history[i][0] <= row["opens_at"]:
            if last is not None:
                repeats += history[i][1] == last
                pairs += 1
            last = history[i][1]
            i += 1
        if last is None:
            continue
        q = (repeats + 1) / (pairs + 2)
        out[row["ticker"]] = q if last == 1 else 1 - q
    return out


def versus_market(settled, history):
    """Brier/log loss of recorded probabilities vs Kalshi pre-open mid and trivial baselines,
    on exactly the same contracts, with one-sided HLN Diebold-Mariano tests."""
    common = [r for r in settled if r["up"] is not None and r["kalshi_preopen_mid"] is not None]
    base = expanding_base_rate(settled, history)
    keep = persistence(settled, history)
    result = {"contracts_with_both": len(common),
              "contracts_without_preopen_mid": sum(r["kalshi_preopen_mid"] is None for r in settled)}
    if len(common) < 2:
        result["status"] = "INSUFFICIENT_DATA"
        return result
    y = [r["outcome"] for r in common]
    sources = {
        "recorded_ai_opening": [r["up"] for r in common],
        "kalshi_preopen_mid": [r["kalshi_preopen_mid"] for r in common],
        "constant_0.5": [0.5] * len(common),
        "expanding_base_rate": [base[r["ticker"]] for r in common],
    }
    if all(r["ticker"] in keep for r in common):
        sources["persistence_last_settled"] = [keep[r["ticker"]] for r in common]
    result["scores"] = {
        name: {k: score(p, y)[k] for k in ("brier", "log_loss", "accuracy", "expected_calibration_error")}
        for name, p in sources.items()
    }
    ai = rigor._log_losses(sources["recorded_ai_opening"], y)
    market = rigor._log_losses(sources["kalshi_preopen_mid"], y)
    brier_ai = [(p - t) ** 2 for p, t in zip(sources["recorded_ai_opening"], y)]
    brier_mk = [(p - t) ** 2 for p, t in zip(sources["kalshi_preopen_mid"], y)]
    result["ai_better_than_market_log_loss"] = rigor.paired_superiority_test(ai, market)
    result["market_better_than_ai_log_loss"] = rigor.paired_superiority_test(market, ai)
    result["ai_better_than_market_brier"] = rigor.paired_superiority_test(brier_ai, brier_mk)
    result["status"] = "MEASURED"
    return result


def sealed_split(settled):
    """Chronological 80/20 split by opening time; holdout identity is hashed."""
    n = len(settled)
    cut = n - int(round(n * SEALED_HOLDOUT_FRACTION))
    development, holdout = settled[:cut], settled[cut:]
    digest = hashlib.sha256("\n".join(r["ticker"] for r in holdout).encode()).hexdigest()
    return development, holdout, {
        "method": f"last {SEALED_HOLDOUT_FRACTION:.0%} of settled openings by opens_at",
        "development_contracts": len(development),
        "holdout_contracts": len(holdout),
        "holdout_starts_at": holdout[0]["opens_at"].isoformat() if holdout else None,
        "holdout_ticker_sha256": digest,
    }


def hypothetical_pnl(db, calls):
    """Displayed calls priced at the first executable ask of their side after the call."""
    from .edge_scanner import quantity_fee
    from .opening_research import QUOTE_WINDOW_SECONDS, SLIPPAGE, _quote_at, _snapshot_quote_at

    tables = _tables(db)
    priced, unpriced, assumed = [], 0, 0
    for r in calls:
        row = {"ticker": r["ticker"], "timestamp": r["generated_at"]}
        quote = None
        try:
            if "opening_quote_snapshots" in tables:
                quote = _snapshot_quote_at(db, row)
            if quote is None and "predictions" in tables:
                quote = _quote_at(db, row)
        except (TypeError, ValueError, AttributeError, OverflowError):
            quote = None
        if quote is None:
            unpriced += 1
            continue
        side_up = r["direction"] == "UP"
        ask = quote["up_ask"] if side_up else quote["down_ask"]
        cost = ask + quantity_fee(ask, 1, quote["fee_multiplier"]) + SLIPPAGE
        if cost >= 1:
            unpriced += 1
            continue
        assumed += bool(quote["fee_multiplier_assumed"])
        won = (r["outcome"] == 1) == side_up
        priced.append({"day": r["opens_at"].date().isoformat(), "pnl": (1.0 if won else 0.0) - cost,
                       "cost": cost, "won": won})
    return pnl_summary(priced, unpriced, assumed, QUOTE_WINDOW_SECONDS, SLIPPAGE)


def pnl_summary(priced, unpriced, assumed=0, window=60, slippage=0.01):
    out = {
        "label": "HYPOTHETICAL: 1 contract per displayed call at the top-of-book ask; not a trade record",
        "priced_calls": len(priced),
        "excluded_no_executable_ask": unpriced,
        "fee_multiplier_assumed": assumed,
        "costs": f"ask + Kalshi taker fee + {slippage:.2f} slippage; first executable ask within {window}s after the call",
    }
    if not priced:
        out["status"] = "INSUFFICIENT_DATA"
        return out
    pnl = [p["pnl"] for p in priced]
    total, peak, drawdown = 0.0, 0.0, 0.0
    for x in pnl:
        total += x
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    mean_cost = sum(p["cost"] for p in priced) / len(priced)
    out.update(
        mean_net_pnl_per_contract=sum(pnl) / len(pnl),
        total_net_pnl=total,
        hit_rate=sum(p["won"] for p in priced) / len(priced),
        break_even_hit_rate=mean_cost,
        max_drawdown=drawdown,
        day_block_bootstrap_95=day_block_bootstrap(priced),
        status="MEASURED",
    )
    return out


def day_block_bootstrap(priced, replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    """95% interval of mean P&L per contract, resampling whole UTC days."""
    days = {}
    for p in priced:
        days.setdefault(p["day"], []).append(p["pnl"])
    blocks = list(days.values())
    if len(blocks) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(replicates):
        sample = [x for _ in blocks for x in rng.choice(blocks)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return [means[int(0.025 * replicates)], means[int(0.975 * replicates) - 1]]


def audit(db, asof=None):
    asof = asof or datetime.now(timezone.utc)
    rows, problems = load_openings(db, asof)
    settled = [r for r in rows if r["outcome"] is not None]
    history = official_history(db, asof)
    development, holdout, seal = sealed_split(settled)
    report = {
        "format": AUDIT_FORMAT,
        "role": "READ_ONLY_RESEARCH",
        "asof": asof.isoformat(),
        "activation_allowed": False,
        "journal_audit": journal_audit(db, rows, problems, asof),
        "integrity_checks": integrity_checks(db, rows, asof),
        "quote_snapshot_coverage": quote_snapshot_coverage(db, rows),
        "daily_breakdown": daily_breakdown(rows),
        "model_versions": model_versions(settled),
        "dashboard_scoreboard_replica": dashboard_scoreboard(rows),
        "sealed_holdout": seal,
        "all_settled": {
            "displayed_accuracy": displayed_accuracy(settled),
            "calibration": calibration(settled),
            "versus_market": versus_market(settled, history),
            "hypothetical_pnl": hypothetical_pnl(db, [r for r in settled if displayed(r)]),
        },
        "development": {
            "no_call_reasons": no_call_reasons(development),
            "displayed_accuracy": displayed_accuracy(development),
            "calibration": calibration(development),
        },
        "holdout": {
            "no_call_reasons": no_call_reasons(holdout),
            "displayed_accuracy": displayed_accuracy(holdout),
            "calibration": calibration(holdout),
            "versus_market": versus_market(holdout, history),
            "hypothetical_pnl": hypothetical_pnl(db, [r for r in holdout if displayed(r)]),
        },
        "phase4_threshold_challenge": threshold_challenge(settled),
        "note": (
            "The recorded AI probabilities were fixed before settlement, so the full "
            "record is already out-of-sample for the live model. The sealed holdout "
            "is the slice reserved for judging any NEW candidate; it is reported "
            "here only so the same slice can be reused unchanged."
        ),
    }
    return report


def summary_lines(report):
    j = report["journal_audit"]
    a = report["all_settled"]
    d, c, m, p = a["displayed_accuracy"], a["calibration"], a["versus_market"], a["hypothetical_pnl"]
    fmt = lambda v, n=4: "n/a" if v is None else (f"{v:.{n}f}" if isinstance(v, float) else str(v))
    lines = [
        f"AUDIT recorded_openings={j['recorded_openings']} settled={j['verified_settled_openings']} "
        f"awaiting={j['awaiting_settlement']} missing_settlement={j['missing_settlement_after_grace']} "
        f"missing_slots={j['missing_15_minute_slots_between_first_and_last']} range={j['first_opening']}..{j['last_opening']}",
        f"DISPLAYED calls={d['displayed_calls']} wait={d['withheld_or_wait']} accuracy={fmt(d['accuracy'])} "
        f"wilson95={d['accuracy_wilson_95']} up={d['up_calls']['calls']}/{fmt(d['up_calls']['accuracy'])} "
        f"down={d['down_calls']['calls']}/{fmt(d['down_calls']['accuracy'])}",
        f"CALIBRATION n={c.get('samples')} brier={fmt(c.get('brier'))} logloss={fmt(c.get('log_loss'))} "
        f"ece={fmt(c.get('expected_calibration_error'))} slope={fmt((c.get('calibration_slope') or {}).get('slope'))}",
    ]
    for band, s in d["by_confidence_band"].items():
        if s["calls"]:
            lines.append(f"  band {band}: calls={s['calls']} acc={fmt(s['accuracy'])} wilson95={s['accuracy_wilson_95']}")
    if m.get("status") == "MEASURED":
        lines.append("VS_MARKET n=%d " % m["contracts_with_both"] + " ".join(
            f"{k}:brier={fmt(v['brier'])}" for k, v in m["scores"].items()))
        lines.append(f"  AI<market logloss p={fmt(m['ai_better_than_market_log_loss']['p_value'])} "
                     f"market<AI p={fmt(m['market_better_than_ai_log_loss']['p_value'])}")
    else:
        lines.append(f"VS_MARKET {m.get('status')} contracts_with_both={m.get('contracts_with_both')}")
    lines.append(f"PNL priced={p['priced_calls']} excluded={p['excluded_no_executable_ask']} "
                 f"mean={fmt(p.get('mean_net_pnl_per_contract'))} total={fmt(p.get('total_net_pnl'))} "
                 f"boot95={p.get('day_block_bootstrap_95')} breakeven={fmt(p.get('break_even_hit_rate'))}")
    board = report.get("dashboard_scoreboard_replica") or {}
    if board:
        lines.append(f"SCOREBOARD_REPLICA {board['correct']}-{board['wrong']} acc={fmt(board['accuracy'])} "
                     f"wilson95={board['accuracy_wilson_95']} single_model={board['single_model']} "
                     f"from={board['first_call_opens_at']} to={board['last_call_opens_at']}")
        recent = board.get("most_recent_26")
        if recent:
            lines.append(f"  most_recent_26 {recent['correct']}-{recent['wrong']} from={recent['from']} models={recent['models']}")
    for model, v in (report.get("model_versions") or {}).items():
        lines.append(f"  model {model}: {v['first_opening'][:16]}..{v['last_opening'][:16]} settled={v['settled_openings']} "
                     f"calls={v['displayed_calls']} acc={fmt(v['accuracy'])} wilson95={v['accuracy_wilson_95']} "
                     f"brier={fmt(v['brier_all_recorded'])}")
    challenge = report.get("phase4_threshold_challenge") or {}
    fixed = challenge.get("fixed_policy_on_sealed_holdout") or {}
    if fixed:
        lines.append(f"THRESHOLD_0.55_HOLDOUT scope={challenge.get('model_scope')} calls={fixed.get('calls')} "
                     f"acc={fmt(fixed.get('accuracy'))} wilson95={fixed.get('accuracy_wilson_95')} coverage={fmt(fixed.get('coverage'))}")
        for model, sub in (challenge.get("by_model") or {}).items():
            f = sub.get("fixed_policy_on_sealed_holdout") or {}
            lines.append(f"  {model}: holdout calls={f.get('calls')} acc={fmt(f.get('accuracy'))} wilson95={f.get('accuracy_wilson_95')}")
    quarantine = (report.get("journal_audit") or {}).get("unparseable", {}).get("CONFLICTING_OR_INVALID_OFFICIAL_OUTCOME")
    if quarantine:
        lines.append(f"QUARANTINED_SETTLEMENTS {quarantine}")
    q = report.get("quote_snapshot_coverage") or {}
    lines.append(f"QUOTES table={q.get('table_present')} rows={q.get('rows')} preopen_rows={q.get('rows_at_or_before_open')} "
                 f"first={q.get('first_observed_at')} settled_with_preopen={q.get('settled_openings_with_preopen_quote')}")
    lines.append(f"HOLDOUT_NO_CALL {report['holdout']['no_call_reasons']['reasons']} "
                 f"models={report['holdout']['no_call_reasons']['models_seen']}")
    h = report["holdout"]["displayed_accuracy"]
    lines.append(f"SEALED_HOLDOUT n={report['sealed_holdout']['holdout_contracts']} from={report['sealed_holdout']['holdout_starts_at']} "
                 f"calls={h['displayed_calls']} accuracy={fmt(h['accuracy'])} wilson95={h['accuracy_wilson_95']}")
    return lines


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: -item[1]))

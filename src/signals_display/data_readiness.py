"""Read-only opening data-quality and research-readiness audit. RESEARCH ONLY.

Phase 5. Explains, from the journal alone (opened read-only by the caller):

* why settled contracts have no opening probability, separating openings made
  before the current model was active from genuine collection failures, and
  in-process failures (inputs not ready within the 120 s grace) from openings
  created late (process not running/ticking at the open, e.g. a restart);
* whether a failed opening's 24 h candles were actually recorded by the open
  (a timing/snapshot defect) or genuinely missing;
* opening pipeline timing: history and reference age at open, generation delay,
  optional-context status and per-reason exclusions;
* why individual model inputs are sparse;
* whether any executable Kalshi quote was observed before an opening;
* candle and feed collection gaps;
* how many more eligible openings Report B needs, at the observed rate;
* threshold-based ALERT lines for monitoring.

Nothing is written to the journal, nothing is backfilled or repaired, and no
model, threshold, service or order is touched.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sqlite3

from .model import timestamp

READINESS_FORMAT = "kxbtc-opening-data-readiness-v1"
MODE = "PUBLIC MARKET DATA"
GRACE_SECONDS = 120
SPARSE = 0.5
CONTRACT_SECONDS = 900
# Report B needs WALK_START contracts before the first fold plus at least two
# development blocks of out-of-sample calls, and the last 20 % is sealed.
REPORT_B_DEVELOPMENT_NEEDED = 170 + 2 * 30
SEALED_FRACTION = 0.20

REASON_PATTERNS = (
    (r"Incomplete 24h window", "CANDLES_INCOMPLETE_24H"),
    (r"Need 1440 contiguous", "CANDLES_INSUFFICIENT"),
    (r"Candle receipt stale", "CANDLES_RECEIPT_STALE"),
    (r"Latest complete historical minute is too old", "CANDLES_LAST_MINUTE_TOO_OLD"),
    (r"Historical snapshot received after opening", "CANDLES_RECEIVED_AFTER_OPEN"),
    (r"Unaligned candle|Invalid OHLC|Duplicate candle interval|No measured variation", "CANDLES_INVALID"),
    (r"No fresh BTC reference", "REFERENCE_MISSING_AT_OPEN"),
    (r"Conflicting pre-opening reference", "REFERENCE_CONFLICT"),
    (r"Unsupported official settlement rules", "SETTLEMENT_RULES_UNVERIFIED"),
    (r"Incompatible contract duration", "CONTRACT_DURATION"),
    (r"generated during the active contract", "GENERATED_OUTSIDE_CONTRACT"),
    (r"OPENING_STORAGE_UNAVAILABLE", "STORAGE_BUSY"),
    (r"OPENING_GENERATION_ERROR", "GENERATION_ERROR"),
    (r"No valid frozen opening inputs", "NO_FROZEN_INPUTS"),
    (r"^'[a-z_]+'$", "INPUT_FIELD_MISSING"),
)
CANDLE_CATEGORIES = {"CANDLES_INCOMPLETE_24H", "CANDLES_INSUFFICIENT", "CANDLES_RECEIPT_STALE",
                     "CANDLES_LAST_MINUTE_TOO_OLD", "CANDLES_RECEIVED_AFTER_OPEN", "NO_FROZEN_INPUTS"}
SOURCE_GROUPS = (
    (("spot_cvd", "perpetual_cvd", "spot_book", "spot_micro", "spot_depth", "spot_delta", "perpetual_delta"),
     "kraken_order_flow", "Pre-open Kraken flow must be LIVE and at most 5 s old at the open"),
    (("advanced_",), "advanced_flow", "Advanced flow/indicator events must be received before the open"),
    (("derivatives_basis",), "perpetual_basis", "Needs an as-of perpetual trade price before the open"),
    (("micro_",), "reference_ticks", "Needs pre-open BTC reference ticks within 65 s of the open"),
    (("kalshi_",), "kalshi_quotes", "Needs a two-sided Kalshi quote at or before the open"),
)


def _tables(db):
    return {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _ts(value):
    try:
        return timestamp(value)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def classify_reason(reason):
    text = str(reason or "")
    for pattern, category in REASON_PATTERNS:
        if re.search(pattern, text):
            return category
    return "OTHER"


def _percentiles(values):
    values = sorted(v for v in values if v is not None and math.isfinite(v))
    if not values:
        return None
    pick = lambda q: values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))]
    return {"n": len(values), "min": values[0], "p50": pick(0.5), "p90": pick(0.9),
            "p99": pick(0.99), "max": values[-1]}


def _count(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def load(db, asof):
    """Recorded openings joined to trusted official labels (quarantine applied)."""
    from .settled_audit import official_outcomes

    if "btc_engine_opening" not in _tables(db):
        return [], {}
    outcomes, quarantined = official_outcomes(db)
    rows = []
    for ticker, generated, closes, up, payload in db.execute(
        "SELECT ticker,generated_at,closes_at,up,payload FROM btc_engine_opening WHERE mode=?", (MODE,)
    ):
        try:
            saved = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            saved = {}
        opened = _ts(saved.get("opens_at"))
        if opened is None or opened > asof:
            continue
        observed, outcome = outcomes.get(ticker, (None, None))
        label_at = _ts(observed)
        settled = outcome in (0, 1) and label_at is not None and label_at <= asof
        rows.append({
            "ticker": ticker, "opens_at": opened, "generated_at": _ts(generated),
            "closes_at": _ts(closes), "up": up if type(up) in (int, float) and math.isfinite(up) else None,
            "saved": saved, "settled": settled, "outcome": outcome if settled else None,
            "quarantined": ticker in quarantined,
        })
    rows.sort(key=lambda r: (r["opens_at"], r["ticker"]))
    return rows, outcomes


def _delay(row):
    saved = row["saved"]
    delay = saved.get("opening_delay_seconds")
    if type(delay) in (int, float) and math.isfinite(delay):
        return float(delay)
    if row["generated_at"] is not None:
        return (row["generated_at"] - row["opens_at"]).total_seconds()
    return None


def activation_time(rows, artifact_available_at=None):
    """When the current opening model began making calls (journal-inferred)."""
    if artifact_available_at:
        return _ts(artifact_available_at), "artifact_available_at"
    for row in rows:
        if str(row["saved"].get("model", "")).startswith("opening-candidate/") and row["up"] is not None:
            return row["opens_at"], "first_opening_by_an_opening_candidate_model"
    return None, "no_opening_candidate_model_seen"


def _candles_recorded_by_open(db, opened):
    """Distinct Coinbase minutes received by the open, in context24's own window.

    Mirrors context24: the window ends at the latest complete minute RECEIVED
    by the open (it must be within 120 s of the open), and covers the 1,440
    minutes before that cutoff. The minute that closes exactly at the open is
    normally received just after it, so the window usually ends one minute
    earlier. Returns 0 when no recent complete minute was received in time.
    """
    if "btc_candles" not in _tables(db):
        return None
    open_ts = int(opened.timestamp())
    received_by_open = set()
    for source, minute, received in db.execute(
        "SELECT source,start,first_received_at FROM btc_candles WHERE start>=? AND start<?",
        (open_ts - 86400 - 180, open_ts),
    ):
        if not str(source).startswith("Coinbase"):
            continue
        stamp = _ts(received)
        if stamp is not None and stamp <= opened and minute + 60 <= open_ts:
            received_by_open.add(minute)
    if not received_by_open:
        return 0
    cutoff = max(received_by_open) + 60
    if open_ts - cutoff > 120:
        return 0
    return sum(1 for minute in received_by_open if cutoff - 86400 <= minute < cutoff)


def _reference_observations_by_open(db, opened):
    """Raw reference-feed rows observed in the 65 s before the open (candidate evidence)."""
    if "feed_observations" not in _tables(db):
        return None
    rows = db.execute(
        """SELECT COUNT(*) FROM feed_observations
           WHERE julianday(timestamp)>=julianday(?) AND julianday(timestamp)<=julianday(?)
             AND (source LIKE '%Coinbase%' OR source LIKE '%Kraken%')""",
        ((opened - timedelta(seconds=65)).isoformat(), opened.isoformat()),
    ).fetchone()
    return rows[0]


def unavailable_openings(db, rows, asof, activation):
    settled = [r for r in rows if r["settled"]]
    missing = [r for r in settled if r["up"] is None]
    activated_at, activation_source = activation
    entries, by_category, by_timing, by_phase = [], {}, {}, {}
    for row in missing:
        reason = row["saved"].get("reason") or row["saved"].get("status")
        category = classify_reason(reason)
        delay = _delay(row)
        timing = ("UNKNOWN_DELAY" if delay is None
                  else "SAVED_BEFORE_GRACE_DEADLINE" if delay < GRACE_SECONDS - 10
                  else "IN_PROCESS_AT_GRACE_DEADLINE" if delay <= GRACE_SECONDS + 80
                  else "CREATED_LATE_PROCESS_NOT_TICKING_AT_OPEN")
        phase = ("UNKNOWN" if activated_at is None
                 else "BEFORE_CURRENT_MODEL_ACTIVE" if row["opens_at"] < activated_at
                 else "AFTER_CURRENT_MODEL_ACTIVE")
        evidence = {}
        if category in CANDLE_CATEGORIES:
            recorded = _candles_recorded_by_open(db, row["opens_at"])
            evidence["coinbase_minutes_recorded_by_open"] = recorded
            evidence["candle_diagnosis"] = (
                None if recorded is None
                else "RECORDED_BY_OPEN_TIMING_OR_SNAPSHOT_DEFECT" if recorded >= 1440
                else "GENUINELY_MISSING_AT_OPEN"
            )
        if category == "REFERENCE_MISSING_AT_OPEN":
            count = _reference_observations_by_open(db, row["opens_at"])
            evidence["reference_feed_rows_in_65s_before_open"] = count
            evidence["reference_diagnosis"] = (
                None if count is None
                else "FEED_ROWS_EXIST_CANDIDATE_RECOVERY_EVIDENCE" if count
                else "NO_FEED_ROWS_RECORDED"
            )
        by_category[category] = by_category.get(category, 0) + 1
        by_timing[timing] = by_timing.get(timing, 0) + 1
        by_phase[phase] = by_phase.get(phase, 0) + 1
        entries.append({"ticker": row["ticker"], "opens_at": row["opens_at"].isoformat(),
                        "category": category, "timing": timing, "phase": phase,
                        "delay_seconds": delay, "reason": str(reason)[:240], **evidence})
    genuine = [e for e in entries if e["phase"] != "BEFORE_CURRENT_MODEL_ACTIVE"]
    diag = _count(e.get("candle_diagnosis") or e.get("reference_diagnosis") or "NOT_APPLICABLE" for e in entries)
    without_opening = 0
    for ticker in set(_settled_tickers(db, asof)) - {r["ticker"] for r in rows}:
        without_opening += 1
    return {
        "settled_contracts_with_opening_record": len(settled),
        "settled_without_probability": len(missing),
        "settled_contracts_with_no_opening_record": without_opening,
        "current_model_activation": {"at": activated_at.isoformat() if activated_at else None,
                                     "source": activation_source},
        "by_phase": by_phase,
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "by_timing": by_timing,
        "evidence_diagnosis": diag,
        "genuine_collection_failures_after_activation": len(
            [e for e in genuine if e["phase"] == "AFTER_CURRENT_MODEL_ACTIVE"]),
        "by_day": _count(e["opens_at"][:10] for e in entries),
        "records": entries[-60:],
        "note": ("A probability-less opening is immutable by design: it is never replaced by a later "
                 "attempt. These counts explain causes; nothing is backfilled."),
    }


def _settled_tickers(db, asof):
    from .settled_audit import official_outcomes

    outcomes, _ = official_outcomes(db)
    return [t for t, (observed, _y) in outcomes.items() if (_ts(observed) or asof) <= asof]


def pipeline_timing(rows):
    made = [r for r in rows if r["up"] is not None]
    history_age, reference_age, delays, bars = [], [], [], []
    context, excluded = {}, {}
    for row in made:
        saved = row["saved"]
        frozen = saved.get("frozen_features") or {}
        quality = saved.get("data_quality") or {}
        received = _ts(frozen.get("history_received_at") or quality.get("history_received_at"))
        if received is not None:
            history_age.append((row["opens_at"] - received).total_seconds())
        age = quality.get("reference_age_at_open_seconds")
        if type(age) in (int, float):
            reference_age.append(float(age))
        delays.append(_delay(row))
        bars.append(quality.get("closed_history_bars"))
        status = str(frozen.get("optional_context_status") or "NOT_RECORDED")
        head = status.split(":")[0].split(";")[0].strip()
        context[head] = context.get(head, 0) + 1
        match = re.search(r"individually excluded: (.*)$", status)
        if match:
            for part in match.group(1).split(", "):
                name, _, value = part.partition("=")
                try:
                    excluded[name] = excluded.get(name, 0) + int(value)
                except ValueError:
                    excluded[name] = excluded.get(name, 0) + 1
    return {
        "openings_with_probability": len(made),
        "history_age_at_open_seconds": _percentiles(history_age),
        "reference_age_at_open_seconds": _percentiles(reference_age),
        "generation_delay_seconds": _percentiles(delays),
        "closed_history_bars": _count(str(b) for b in bars),
        "optional_context_status": dict(sorted(context.items(), key=lambda kv: -kv[1])),
        "optional_exclusions_by_reason": dict(sorted(excluded.items(), key=lambda kv: -kv[1])),
        "per_feature_status_recorded": sum(bool((r["saved"].get("frozen_features") or {}).get(
            "optional_feature_status")) for r in made),
    }


def _group(name):
    for prefixes, group, rule in SOURCE_GROUPS:
        if any(name.startswith(p) for p in prefixes):
            return group, rule
    return "candle_or_reference_core", "Computed from the frozen 24 h candles and reference ticks"


def sparse_inputs(rows, inputs=None):
    """Coverage of each model input in frozen openings, with a diagnosis."""
    made = [r for r in rows if r["up"] is not None and (r["saved"].get("frozen_features") or {}).get("values")]
    if not made:
        return {"status": "NO_FROZEN_OPENINGS"}
    if inputs is None:
        from .features import FEATURES
        inputs = list(FEATURES)
    report, recent_cut = {}, made[-96:]  # last ~24 h of openings
    no_context = sum(str((r["saved"]["frozen_features"]).get("optional_context_status", "")).startswith("UNAVAILABLE")
                     for r in made)
    for name in inputs:
        keyed = [r for r in made if name in r["saved"]["frozen_features"]["values"]]
        present = [r for r in keyed if type(r["saved"]["frozen_features"]["values"][name]) in (int, float)]
        share = len(present) / len(made)
        if share >= SPARSE:
            continue
        group, rule = _group(name)
        statuses = _count(str(((r["saved"]["frozen_features"].get("optional_feature_status") or {}).get(name))
                              ) for r in made)
        recent = sum(type(r["saved"]["frozen_features"]["values"].get(name)) in (int, float) for r in recent_cut)
        if not keyed:
            diagnosis = "NOT_RECORDED_BY_THE_LIVE_CODE: the frozen record has no slot for this input (vocabulary mismatch)"
        elif group == "candle_or_reference_core":
            diagnosis = ("SUSPECT_COLLECTION_OR_RECORDING: a core candle/reference input should be present "
                         "whenever the opening has a probability")
        elif no_context >= len(made) / 2:
            diagnosis = "SOURCE_CONTEXT_ABSENT_AT_OPEN: most frozen openings had no optional context at all"
        else:
            diagnosis = f"GENUINELY_UNAVAILABLE_OR_EXCLUDED: {rule}"
        report[name] = {
            "coverage": share, "present": len(present), "examined": len(made),
            "recent_24h_coverage": recent / len(recent_cut) if recent_cut else None,
            "first_present": present[0]["opens_at"].isoformat() if present else None,
            "source_group": group, "collection_rule": rule,
            "recorded_per_feature_status": statuses, "diagnosis": diagnosis,
        }
    return {"examined_openings": len(made), "sparse_threshold": SPARSE,
            "openings_without_optional_context": no_context, "sparse_inputs": report}


def quote_coverage(db):
    if "opening_quote_snapshots" not in _tables(db):
        return {"table_present": False,
                "diagnosis": "Quote capture is not present in this journal's code version."}
    buckets = {"before_-30s": 0, "-30s_to_0s": 0, "0s_to_10s": 0, "10s_to_60s": 0, "60s_to_120s": 0, "other": 0}
    earliest, valid_pre, spreads = {}, 0, []
    for ticker, seconds, up_bid, up_ask, down_ask in db.execute(
        "SELECT ticker,seconds_from_open,up_bid,up_ask,down_ask FROM opening_quote_snapshots WHERE mode=?", (MODE,)
    ):
        if type(seconds) not in (int, float):
            buckets["other"] += 1
            continue
        key = ("before_-30s" if seconds < -30 else "-30s_to_0s" if seconds <= 0 else "0s_to_10s" if seconds <= 10
               else "10s_to_60s" if seconds <= 60 else "60s_to_120s" if seconds <= 120 else "other")
        buckets[key] += 1
        earliest[ticker] = min(earliest.get(ticker, seconds), seconds)
        two_sided = all(type(v) in (int, float) and 0 < v < 1 for v in (up_bid, up_ask, down_ask))
        if seconds <= 0 and two_sided and up_bid <= up_ask:
            valid_pre += 1
        if two_sided and up_bid <= up_ask:
            spreads.append(up_ask - up_bid)
    first_seen = _percentiles(list(earliest.values()))
    return {
        "table_present": True, "contracts_with_any_quote": len(earliest),
        "rows_by_seconds_from_open": buckets,
        "executable_two_sided_rows_at_or_before_open": valid_pre,
        "first_quote_seconds_from_open": first_seen,
        "up_spread": _percentiles(spreads),
        "diagnosis": (
            "PRE_OPEN_EXECUTABLE_QUOTES_OBSERVED" if valid_pre else
            "NO_PRE_OPEN_QUOTE_ROWS: the next contract is not in the collector's 'open'/'active' market list "
            "before its opening, or Kalshi does not trade it before the open (run the pre-open probe)"
            if not (buckets["before_-30s"] + buckets["-30s_to_0s"]) else
            "PRE_OPEN_ROWS_WITHOUT_EXECUTABLE_TWO_SIDED_BOOK"
        ),
    }


def collection_health(db, asof, days=7):
    out = {}
    if "btc_candles" in _tables(db):
        start = int((asof - timedelta(days=days)).timestamp()) // 60 * 60
        end = int(asof.timestamp()) // 60 * 60
        minutes, lateness = set(), []
        for source, minute, received in db.execute(
            "SELECT source,start,first_received_at FROM btc_candles WHERE start>=? AND start<?", (start, end)
        ):
            if not str(source).startswith("Coinbase"):
                continue
            minutes.add(minute)
            stamp = _ts(received)
            if stamp is not None:
                lateness.append(stamp.timestamp() - (minute + 60))
        per_day = {}
        for minute in range(start, end, 60):
            if minute not in minutes:
                day = datetime.fromtimestamp(minute, timezone.utc).date().isoformat()
                per_day[day] = per_day.get(day, 0) + 1
        last_day = sum(1 for m in range(max(start, end - 86400), end, 60) if m not in minutes)
        out["candles"] = {"window_days": days, "missing_minutes_by_day": per_day,
                          "missing_minutes_last_24h": last_day,
                          "receipt_lateness_seconds": _percentiles(lateness)}
    if "feed_observations" in _tables(db):
        gaps = {}
        since = (asof - timedelta(hours=24)).isoformat()
        last = {}
        for source, stamp in db.execute(
            "SELECT source,timestamp FROM feed_observations WHERE julianday(timestamp)>=julianday(?) "
            "ORDER BY source,timestamp", (since,)
        ):
            t = _ts(stamp)
            if t is None:
                continue
            if source in last:
                gap = (t - last[source]).total_seconds()
                gaps[source] = max(gaps.get(source, 0.0), gap)
            last[source] = t
        out["feeds_last_24h"] = {s: {"max_gap_seconds": gaps.get(s), "last_seen": last[s].isoformat(),
                                     "age_at_asof_seconds": (asof - last[s]).total_seconds()} for s in last}
    return out


def readiness_projection(db, rows, asof):
    from .opening_models import dataset_coverage

    coverage = dataset_coverage(db, asof)
    eligible = coverage["eligible_frozen_openings"]
    needed = 0
    while needed - int(round(needed * SEALED_FRACTION)) < REPORT_B_DEVELOPMENT_NEEDED:
        needed += 1
    recent = [r for r in rows if r["opens_at"] > asof - timedelta(hours=48)]
    rate = sum(r["up"] is not None and r["settled"] for r in recent) / 2.0
    remaining = max(0, needed - eligible)
    return {
        "eligible_frozen_openings": eligible,
        "official_settled_contracts": coverage["official_settled_contracts"],
        "exclusion_reasons": coverage["exclusion_reasons"],
        "eligible_needed_for_report_b": needed,
        "remaining": remaining,
        "settled_probability_openings_per_day_last_48h": rate,
        "contracts_per_day_maximum": 86400 // CONTRACT_SECONDS,
        "estimated_days_at_recent_rate": (remaining / rate) if rate > 0 else None,
        "note": ("Rate counts settled openings with a probability; feature-version and receipt "
                 "checks can make a few ineligible. Projection, not a guarantee."),
    }


def alerts(report):
    out = []

    def add(severity, code, message):
        out.append({"severity": severity, "code": code, "message": message})

    u = report["unavailable_openings"]
    settled = u["settled_contracts_with_opening_record"]
    if settled:
        rate = u["settled_without_probability"] / settled
        if rate > 0.20:
            add("CRITICAL", "OPENING_PROBABILITY_UNAVAILABLE_RATE", f"{rate:.0%} of settled openings have no probability")
        elif rate > 0.05:
            add("WARN", "OPENING_PROBABILITY_UNAVAILABLE_RATE", f"{rate:.0%} of settled openings have no probability")
    late = u["by_timing"].get("CREATED_LATE_PROCESS_NOT_TICKING_AT_OPEN", 0)
    if late:
        add("WARN", "OPENINGS_CREATED_LATE", f"{late} openings were created after the grace window (process not ticking at the open, e.g. restart)")
    if u["settled_contracts_with_no_opening_record"]:
        add("WARN", "SETTLED_WITHOUT_OPENING_RECORD", f"{u['settled_contracts_with_no_opening_record']} settled contracts have no opening record")
    candles = (report["collection_health"].get("candles") or {})
    if candles.get("missing_minutes_last_24h"):
        add("WARN", "CANDLE_GAPS_LAST_24H", f"{candles['missing_minutes_last_24h']} Coinbase minutes missing in the last 24 h")
    for source, feed in (report["collection_health"].get("feeds_last_24h") or {}).items():
        if (feed.get("max_gap_seconds") or 0) > 120:
            add("WARN", "FEED_GAP", f"{source}: largest gap {feed['max_gap_seconds']:.0f}s in the last 24 h")
    q = report["quote_coverage"]
    if not q.get("executable_two_sided_rows_at_or_before_open"):
        add("WARN", "NO_PRE_OPEN_EXECUTABLE_QUOTES", q.get("diagnosis", "no pre-open quotes"))
    sparse = (report["sparse_inputs"].get("sparse_inputs") or {})
    if sparse:
        add("INFO", "SPARSE_MODEL_INPUTS", f"{len(sparse)} inputs present in <50% of frozen openings: {', '.join(sorted(sparse))}")
    suspect = [n for n, s in sparse.items() if s["diagnosis"].startswith(("SUSPECT", "NOT_RECORDED"))]
    if suspect:
        add("WARN", "INPUT_RECORDING_SUSPECT", "Core or unrecorded inputs sparse: " + ", ".join(sorted(suspect)))
    return out


def audit(db, asof=None, artifact=None):
    asof = asof or datetime.now(timezone.utc)
    rows, _ = load(db, asof)
    inputs = None
    available = None
    if artifact:
        model = artifact.get("model") or {}
        parts = model.get("components") or [model]
        inputs = sorted({n for p in parts for n in ((p.get("preprocessor") or {}).get("names") or [])})
        available = artifact.get("available_at")
    report = {
        "format": READINESS_FORMAT, "role": "READ_ONLY_DATA_QUALITY", "asof": asof.isoformat(),
        "recorded_openings": len(rows),
        "unavailable_openings": unavailable_openings(db, rows, asof, activation_time(rows, available)),
        "pipeline_timing": pipeline_timing(rows),
        "sparse_inputs": sparse_inputs(rows, inputs),
        "quote_coverage": quote_coverage(db),
        "collection_health": collection_health(db, asof),
        "readiness": readiness_projection(db, rows, asof),
        "writes_performed": 0,
    }
    report["alerts"] = alerts(report)
    return report


def summary_lines(report):
    u, r, q = report["unavailable_openings"], report["readiness"], report["quote_coverage"]
    lines = [
        f"DATA_READINESS openings={report['recorded_openings']} settled_with_record={u['settled_contracts_with_opening_record']} "
        f"no_probability={u['settled_without_probability']} settled_without_record={u['settled_contracts_with_no_opening_record']}",
        f"  activation={u['current_model_activation']} by_phase={u['by_phase']}",
        f"  causes={u['by_category']}",
        f"  timing={u['by_timing']} evidence={u['evidence_diagnosis']}",
        f"PIPELINE {json.dumps({k: report['pipeline_timing'][k] for k in ('history_age_at_open_seconds', 'reference_age_at_open_seconds', 'generation_delay_seconds')}, default=str)}",
        f"  optional_context={report['pipeline_timing']['optional_context_status']} exclusions={report['pipeline_timing']['optional_exclusions_by_reason']}",
    ]
    for name, item in (report["sparse_inputs"].get("sparse_inputs") or {}).items():
        lines.append(f"SPARSE {name}: {item['present']}/{item['examined']} recent24h={item['recent_24h_coverage']} "
                     f"group={item['source_group']} -> {item['diagnosis']}")
    lines.append(f"QUOTES {q.get('rows_by_seconds_from_open')} pre_open_executable={q.get('executable_two_sided_rows_at_or_before_open')} -> {q.get('diagnosis')}")
    lines.append(f"READINESS eligible={r['eligible_frozen_openings']}/{r['eligible_needed_for_report_b']} "
                 f"rate/day={r['settled_probability_openings_per_day_last_48h']} days~{r['estimated_days_at_recent_rate']}")
    for alert in report["alerts"]:
        lines.append(f"ALERT {alert['severity']} {alert['code']}: {alert['message']}")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--asof")
    parser.add_argument("--artifact", type=Path, help="Deployed artifact (read only) for its input list")
    args = parser.parse_args()
    from . import rigorous_evaluation as rigor

    try:
        rigor._refuse_foreign_file(args.output, READINESS_FORMAT)
        asof = timestamp(args.asof) if args.asof else datetime.now(timezone.utc)
        artifact = json.loads(args.artifact.read_text()) if args.artifact else None
        with closing(sqlite3.connect(args.journal.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            report = audit(db, asof, artifact)
        from .model_registry import atomic_json

        atomic_json(args.output, report)
        print("DATA_READINESS_BEGIN")
        for line in summary_lines(report):
            print(line)
        print("DATA_READINESS_END")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

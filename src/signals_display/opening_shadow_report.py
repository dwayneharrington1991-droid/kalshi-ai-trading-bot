"""Read-only weekly report: live model vs the shadow challenger on the same eligible openings.

Reads the live journal and the shadow ledger read-only and writes only the report file. Verifies that every shadow
prediction was recorded before its official outcome was observed. Reports actual accuracy and coverage with 95%
intervals and marks weeks with too few resolved calls. It makes no claim of improvement on its own.

Targets are research targets, not assumed results: 85% verified directional accuracy and 90% coverage of eligible
openings. Coverage is calls / all eligible openings. Accuracy is over resolved directional calls only.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import opening_challengers as oc

MIN_CALLS_FOR_CLAIM = 30
TARGET_ACCURACY = 0.85
TARGET_COVERAGE = 0.90


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _week(opens_at):
    iso = _ts(opens_at).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _outcomes(journal_ro):
    out = {}
    for ticker, observed_at, outcome in journal_ro.execute(
            "SELECT ticker, observed_at, outcome FROM forecast_outcomes WHERE origin='official' ORDER BY observed_at"):
        out.setdefault(ticker, (observed_at, int(outcome)))
    return out


def _openings(journal_ro):
    rows = []
    for ticker, payload in journal_ro.execute(
            "SELECT ticker, payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA'"):
        try:
            saved = json.loads(payload)
            rows.append({"ticker": ticker, "opens_at": saved["opens_at"], "live": saved.get("direction")})
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def _metrics(calls, correct, resolved_calls, eligible):
    return {
        "eligible": eligible,
        "calls_all": calls,
        "coverage": round(calls / eligible, 4) if eligible else None,
        "resolved_calls": resolved_calls,
        "correct": correct,
        "accuracy": round(correct / resolved_calls, 4) if resolved_calls else None,
        "accuracy_wilson_95": oc.wilson(correct, resolved_calls),
        "sufficient_sample": resolved_calls >= MIN_CALLS_FOR_CLAIM,
    }


def weekly_report(journal_ro, ledger_path):
    outcomes = _outcomes(journal_ro)
    openings = _openings(journal_ro)
    shadow = {}
    if Path(ledger_path).exists():
        with sqlite3.connect(f"file:{Path(ledger_path).resolve()}?mode=ro", uri=True) as led:
            for ticker, generated_at, scored_at, decision, inputs_complete, seconds, sha in led.execute(
                    "SELECT ticker, generated_at, scored_at, decision, inputs_complete, seconds_after_open, "
                    "model_sha256 FROM shadow_openings"):
                shadow[ticker] = {"generated_at": generated_at, "scored_at": scored_at, "decision": decision,
                                  "complete": inputs_complete, "seconds": seconds, "model": sha}
    weeks = {}
    by_model = {}
    timing = {"scored_after_outcome": 0, "scored_late_over_900s": 0, "excluded_non_prospective": 0}
    for o in openings:
        week = _week(o["opens_at"])
        w = weeks.setdefault(week, {"live": {"calls": 0, "correct": 0, "resolved": 0},
                                    "shadow": {"calls": 0, "correct": 0, "resolved": 0}, "eligible": 0})
        w["eligible"] += 1
        outcome = outcomes.get(o["ticker"])
        for name, decision in (("live", o["live"]), ("shadow", (shadow.get(o["ticker"]) or {}).get("decision"))):
            if decision not in ("UP", "DOWN"):
                continue
            w[name]["calls"] += 1
            if outcome is not None:
                w[name]["resolved"] += 1
                w[name]["correct"] += 1 if (decision == "UP") == (outcome[1] == 1) else 0
        s = shadow.get(o["ticker"])
        if s and outcome is not None and _ts(s["scored_at"]) > _ts(outcome[0]):
            timing["scored_after_outcome"] += 1
            prospective_ok = False
        else:
            prospective_ok = True
        if s and s["seconds"] is not None and s["seconds"] > 900:
            timing["scored_late_over_900s"] += 1
        if s and _ts(s["scored_at"]) < _ts(s["generated_at"]):
            timing["scored_before_generated"] = timing.get("scored_before_generated", 0) + 1
            prospective_ok = False
        if s and prospective_ok and s["decision"] in ("UP", "DOWN") and outcome is not None:
            by_model.setdefault(s["model"], {"resolved": 0, "correct": 0})
            by_model[s["model"]]["resolved"] += 1
            by_model[s["model"]]["correct"] += 1 if (s["decision"] == "UP") == (outcome[1] == 1) else 0
        if s and not prospective_ok:
            timing["excluded_non_prospective"] = timing.get("excluded_non_prospective", 0) + 1
    report_weeks = {}
    for week in sorted(weeks):
        w = weeks[week]
        report_weeks[week] = {
            "live": _metrics(w["live"]["calls"], w["live"]["correct"], w["live"]["resolved"], w["eligible"]),
            "shadow": _metrics(w["shadow"]["calls"], w["shadow"]["correct"], w["shadow"]["resolved"], w["eligible"]),
            "shadow_unscored_openings": w["eligible"] - sum(1 for o in openings if _week(o["opens_at"]) == week
                                                            and o["ticker"] in shadow),
        }
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "targets": {
                "accuracy": TARGET_ACCURACY, "coverage": TARGET_COVERAGE, "note": "research targets, not assumed"},
            "timing_checks": timing,
            "shadow_by_model_version": {k: {**v, "accuracy": round(v["correct"] / v["resolved"], 4) if v["resolved"] else None,
                                             "accuracy_wilson_95": oc.wilson(v["correct"], v["resolved"])}
                                        for k, v in by_model.items()},
            "weeks": report_weeks,
            "claim_policy": "no improvement is claimed for a week below the minimum resolved-call sample"}


def text_summary(report):
    lines = ["Weekly opening performance (live vs shadow challenger, same eligible openings)",
             f"Targets (research, not assumed): accuracy {TARGET_ACCURACY:.0%}, coverage {TARGET_COVERAGE:.0%}",
             f"Timing checks: scored after outcome = {report['timing_checks']['scored_after_outcome']} "
             f"(must be 0); scored late > 900s = {report['timing_checks']['scored_late_over_900s']}"]
    for week, w in report["weeks"].items():
        for name in ("live", "shadow"):
            m = w[name]
            acc = "n/a" if m["accuracy"] is None else f"{m['accuracy']:.1%} ({m['correct']}/{m['resolved_calls']})"
            cov = "n/a" if m["coverage"] is None else f"{m['coverage']:.1%}"
            note = "" if m["sufficient_sample"] else "  [insufficient sample: no claim]"
            lines.append(f"{week} {name:6s} eligible={m['eligible']} accuracy={acc} coverage={cov}{note}")
    return "\n".join(lines)


def _main(argv=None):
    ap = argparse.ArgumentParser(description="read-only weekly live-vs-shadow report")
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{args.journal.resolve()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        report = weekly_report(db, args.ledger)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    (args.out_dir / f"weekly-{stamp}.json").write_text(json.dumps(report, indent=1, sort_keys=True))
    (args.out_dir / f"weekly-{stamp}.txt").write_text(text_summary(report) + "\n")
    print(text_summary(report))


if __name__ == "__main__":
    _main()

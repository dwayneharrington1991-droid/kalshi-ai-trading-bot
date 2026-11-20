"""Read-only dashboard view of the independent shadow ledger. Reads nothing else.

It never substitutes a late, missing or non-prospective prediction, never reconstructs one, and feeds nothing live.
A row counts toward forward accuracy only if the prediction was recorded at or after the opening, before the official
outcome was observed, within the late-window, and the outcome is known. Research targets are shown as targets.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import opening_challengers as oc

LATE_SECONDS = 900
MIN_SETTLED = 30
TARGET_ACCURACY = 0.85
TARGET_COVERAGE = 0.90
RECENT_ROWS = 8

LABEL = "NEW AI — SHADOW (research only; does not affect live signals, alerts, trading or learning)"


def shadow_ledger_path(journal_path):
    """Derived from the journal path, as the lifecycle derives its state folder. No launch argument changes."""
    return Path(journal_path).parent / "shadow" / "shadow_ledger.sqlite3"


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _classify(row):
    (ticker, opens_at, generated_at, scored_at, seconds, complete, probability, decision,
     observed_at, outcome) = row
    prospective = (_ts(scored_at) >= _ts(generated_at)
                   and (observed_at is None or _ts(scored_at) <= _ts(observed_at)))
    if decision not in ("UP", "DOWN", "WAIT") or not complete:
        return {"status": "NO PREDICTION", "counted": False, "direction": None, "prospective": False}
    if seconds is None or seconds > LATE_SECONDS:
        return {"status": "NOT COUNTED — late", "counted": False, "direction": None, "prospective": prospective}
    if not prospective:
        return {"status": "NOT COUNTED — not captured before outcome", "counted": False, "direction": None,
                "prospective": False}
    if outcome is None:
        return {"status": "PENDING", "counted": False, "direction": decision, "prospective": True}
    return {"status": "SETTLED", "counted": True, "direction": decision, "prospective": True}


def ledger_view(ledger_path, now=None):
    now = now or datetime.now(timezone.utc)
    base = {"label": LABEL, "generated_at": now.isoformat(), "live_influence": "NONE",
            "targets": {"accuracy": TARGET_ACCURACY, "coverage": TARGET_COVERAGE,
                        "note": "research targets, not assumed results"}}
    path = Path(ledger_path)
    if not path.exists():
        return {**base, "status": "NOT INSTALLED", "message": "shadow scorer has not produced a ledger yet",
                "recent": [], "forward": None, "counts": {}}
    try:
        with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                "SELECT s.ticker, s.opens_at, s.generated_at, s.scored_at, s.seconds_after_open, "
                "s.inputs_complete, s.probability, s.decision, o.observed_at, o.outcome "
                "FROM shadow_openings s LEFT JOIN shadow_outcomes o ON o.ticker = s.ticker "
                "ORDER BY s.generated_at DESC").fetchall()
    except sqlite3.DatabaseError:
        return {**base, "status": "LEDGER UNREADABLE", "message": "shadow ledger could not be read; live unaffected",
                "recent": [], "forward": None, "counts": {}}
    counts = {"settled": 0, "pending": 0, "no_prediction": 0, "not_counted": 0}
    settled_calls = settled_correct = settled_total = 0
    recent = []
    for row in rows:
        info = _classify(row)
        ticker, opens_at, generated_at, scored_at, _sec, _c, probability, decision, _obs, outcome = row
        if info["status"] == "NO PREDICTION":
            counts["no_prediction"] += 1
        elif info["status"] == "PENDING":
            counts["pending"] += 1
        elif info["status"] == "SETTLED":
            counts["settled"] += 1
        else:
            counts["not_counted"] += 1
        if info["counted"]:
            settled_total += 1
            if info["direction"] in ("UP", "DOWN"):
                settled_calls += 1
                settled_correct += 1 if (info["direction"] == "UP") == (outcome == 1) else 0
        if len(recent) < RECENT_ROWS:
            shown = info["direction"]
            recent.append({"market": ticker, "opens_at": opens_at, "prediction_timestamp": scored_at,
                           "decision": shown if shown in ("UP", "DOWN", "WAIT") else "NO PREDICTION",
                           "probability": round(probability, 4) if (probability is not None and shown) else None,
                           "captured_before_outcome": info["prospective"],
                           "status": info["status"],
                           "outcome": None if outcome is None else ("UP" if outcome == 1 else "DOWN")})
    if settled_total >= MIN_SETTLED:
        forward = {"status": "OK", "settled_markets": settled_total, "directional_calls": settled_calls,
                   "correct": settled_correct,
                   "accuracy": round(settled_correct / settled_calls, 4) if settled_calls else None,
                   "accuracy_wilson_95": oc.wilson(settled_correct, settled_calls),
                   "coverage": round(settled_calls / settled_total, 4)}
    else:
        forward = {"status": "INSUFFICIENT SAMPLE", "settled_markets": settled_total, "needed": MIN_SETTLED}
    return {**base, "status": "OK", "recent": recent, "forward": forward, "counts": counts}

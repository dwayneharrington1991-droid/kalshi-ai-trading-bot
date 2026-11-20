"""Opening data-quality alerts (Phase 5). Read-only; never changes a forecast.

``compute_alerts`` is a pure function over the most recent immutable opening
records, the published candle-integrity report and the latest BTC reference
receipt ages. The server exposes it at ``/api/data-quality-alerts``; it does
not affect /healthz, /readyz, the dashboard or any prediction.
"""
from datetime import datetime, timezone
import math

from .model import timestamp

STALE_REFERENCE_SECONDS = 10
REPEATED_FAILURES = 2
GRACE_SECONDS = 120


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def input_availability(opening, model_inputs):
    """Per-input availability of one frozen opening for the model's inputs."""
    frozen = (opening or {}).get("frozen_features") or {}
    values = frozen.get("values") or {}
    statuses = frozen.get("optional_feature_status") or {}
    table = {}
    for name in model_inputs:
        if name not in values:
            table[name] = "NOT_RECORDED"
        elif _finite(values[name]):
            table[name] = "PRESENT"
        else:
            table[name] = "MISSING:" + str(statuses.get(name) or (
                "NO_OPTIONAL_CONTEXT" if frozen.get("optional_context_available") is False else "NOT_COMPUTED"))
    present = sum(v == "PRESENT" for v in table.values())
    return {"inputs": len(table), "present": present, "missing": len(table) - present, "by_input": table}


def compute_alerts(openings, candle_report, reference_ages, model_inputs, now=None):
    """openings: recent immutable opening payloads, oldest first."""
    now = now or datetime.now(timezone.utc)
    alerts = []

    def add(severity, code, message, **extra):
        alerts.append({"severity": severity, "code": code, "message": message, **extra})

    decided = []
    for opening in openings:
        try:
            opened = timestamp(opening["opens_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if (now - opened).total_seconds() >= GRACE_SECONDS:
            decided.append(opening)
    if decided and decided[-1].get("up") is None:
        add("WARN", "MISSING_OPENING_PROBABILITY",
            f"{decided[-1].get('ticker')}: no opening probability ({str(decided[-1].get('reason'))[:160]})")
    streak = 0
    for opening in reversed(decided):
        if opening.get("up") is not None:
            break
        streak += 1
    if streak >= REPEATED_FAILURES:
        add("CRITICAL", "REPEATED_OPENING_FAILURES", f"{streak} consecutive openings without a probability",
            consecutive=streak)
    fresh = [age for age in (reference_ages or {}).values() if _finite(age)]
    if not fresh or min(fresh) > STALE_REFERENCE_SECONDS:
        add("WARN", "STALE_BTC_FEED",
            f"newest BTC reference receipt is {min(fresh):.0f}s old" if fresh else "no BTC reference receipt",
            ages=reference_ages)
    status = (candle_report or {}).get("status")
    if status != "READY":
        add("WARN", "CANDLE_WINDOW_NOT_READY",
            f"candle window {status}: {len((candle_report or {}).get('missing_intervals') or [])} missing minutes",
            status=status)
    latest = next((o for o in reversed(decided) if o.get("up") is not None), None)
    availability = input_availability(latest, model_inputs) if latest and model_inputs else None
    if availability and availability["missing"]:
        add("INFO", "MISSING_FEATURE_RECEIPTS",
            f"{latest.get('ticker')}: {availability['missing']}/{availability['inputs']} model inputs missing",
            missing=sorted(k for k, v in availability["by_input"].items() if v != "PRESENT"))
    return {
        "generated_at": now.isoformat(),
        "alerts": alerts,
        "status": "ALERT" if any(a["severity"] in {"WARN", "CRITICAL"} for a in alerts) else "OK",
        "recent_openings": [{"ticker": o.get("ticker"), "opens_at": o.get("opens_at"),
                             "has_probability": o.get("up") is not None,
                             "input_recovery": (o.get("input_recovery") or {}).get("status")} for o in decided[-8:]],
        "latest_opening_model_input_availability": availability,
        "read_only": True,
    }

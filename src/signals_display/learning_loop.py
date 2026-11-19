"""Learning ledger, failure analysis and safe candidate promotion for 8768.

Read-only over the journal (mode=ro, query_only). Writes nothing to the journal,
the live model, the artifact registry or the live server. A candidate can be
PROMOTE_ELIGIBLE on paper; applying it is always a separate, human-approved step.

* ledger:    every PUBLIC MARKET DATA opening, including skipped ones (NO VERIFIED
             EDGE, WAIT, unavailable), with its immutable recorded call, confidence
             status, reason, as-of-opening conditions and the official outcome if it
             was observed by the as-of time. Nothing is ever rewritten.
* patterns:  descriptive failure analysis. A condition is reported only if its
             call accuracy differs from the overall rate by a two-sided exact binomial
             test AND survives a Holm correction across all conditions AND has at least
             MIN_PATTERN_CALLS calls. Labelled "candidate pattern, not a trading rule".
* promotion: a candidate is PROMOTE_ELIGIBLE only if, on unseen (fresh) markets, it has
             at least MIN_FRESH_CONTRACTS settled contracts, its Wilson 95% lower bound
             exceeds the current model's accuracy point estimate, and its log loss is
             lower. Otherwise HOLD. The current model is always kept as the rollback
             reference.
"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile

from .scores import wilson_interval

FORMAT = "kxbtc-learning-loop-8768-v1"
DISPLAYABLE = {"CALIBRATED", "EXPERIMENTAL CALIBRATED CANDIDATE"}
MIN_PATTERN_CALLS = 20
ALPHA = 0.05
MIN_FRESH_CONTRACTS = 100


def _float(value):
    return float(value) if type(value) in (int, float) and value == value and abs(value) != float("inf") else None


def _sign(value, positive, negative):
    if value is None:
        return "unknown"
    return positive if value > 0 else negative if value < 0 else "flat"


def conditions_of(values):
    """Conditions known at the opening (from the immutable frozen features only)."""
    v = values or {}
    rel = _float(v.get("relative_volume"))
    return {
        "momentum_5m": _sign(_float(v.get("return_5m")), "up", "down"),
        "order_flow_cvd": _sign(_float(v.get("spot_cvd_delta_fraction")), "buying", "selling"),
        "volume": "unknown" if rel is None else ("high" if rel >= 1.0 else "low"),
        "trend_ema9_21": _sign(_float(v.get("opening_ema9_21_gap")), "up", "down"),
    }


def build_ledger(db, asof):
    """Rows for every opening record observed by ``asof``; skipped openings included."""
    asof_iso = asof.isoformat()
    rows = db.execute(
        """SELECT b.ticker,b.generated_at,b.closes_at,b.up,b.payload,f.outcome,f.observed_at
           FROM btc_engine_opening b
           LEFT JOIN forecast_outcomes f ON f.ticker=b.ticker AND f.origin='official'
             AND julianday(f.observed_at)<=julianday(?)
           WHERE b.mode='PUBLIC MARKET DATA' AND julianday(b.generated_at)<=julianday(?)
           ORDER BY b.generated_at, b.ticker""",
        (asof_iso, asof_iso),
    ).fetchall()
    ledger = []
    for ticker, generated, closes, up, payload, outcome, observed in rows:
        try:
            d = json.loads(payload) if payload else {}
        except ValueError:
            d = {}
        direction = d.get("direction")
        status = (d.get("confidence") or {}).get("status")
        displayed = direction in ("UP", "DOWN") and status in DISPLAYABLE and _float(up) is not None
        outcome_known = outcome in (0, 1)
        correct = None
        if displayed and outcome_known:
            correct = (direction == "UP") == bool(outcome)
        ledger.append({
            "ticker": ticker, "generated_at": generated, "closes_at": closes, "direction": direction,
            "up": _float(up), "confidence_status": status, "displayed": displayed,
            "skip_reason": None if displayed else (d.get("reason") or "not an UP/DOWN call")[:160],
            "outcome_up": int(outcome) if outcome_known else None,
            "outcome_observed_at": observed, "correct": correct,
            "conditions": conditions_of((d.get("frozen_features") or {}).get("values")),
        })
    return ledger


def _binom_two_sided(k, n, p0):
    """Exact two-sided binomial p-value for k successes in n trials at rate p0."""
    if n == 0:
        return 1.0
    pmf = [math.comb(n, i) * p0 ** i * (1 - p0) ** (n - i) for i in range(n + 1)]
    observed = pmf[k]
    return min(1.0, sum(p for p in pmf if p <= observed * (1 + 1e-9)))


def holm(p_values):
    ordered = sorted(p_values.items(), key=lambda kv: kv[1])
    m, running, out = len(ordered), 0.0, {}
    for rank, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - rank) * p))
        out[name] = running
    return out


def failure_patterns(ledger):
    """Descriptive. Only displayed calls with a known outcome are analysed."""
    scored = [r for r in ledger if r["correct"] is not None]
    n = len(scored)
    report = {"scored_calls": n, "min_pattern_calls": MIN_PATTERN_CALLS, "patterns": [],
              "rule": "Reported only if Holm-adjusted p < 0.05 and >= 20 calls. Descriptive; not a trading rule."}
    if n < MIN_PATTERN_CALLS:
        report["status"] = "INSUFFICIENT_SCORED_CALLS"
        return report
    rate = sum(1 for r in scored if r["correct"]) / n
    p0 = min(max(rate, 1e-6), 1 - 1e-6)
    cells = {}
    for r in scored:
        for name, value in r["conditions"].items():
            cells.setdefault((name, value), []).append(r["correct"])
        momentum = r["conditions"]["momentum_5m"]
        if momentum in ("up", "down"):  # unknown or flat momentum is not classified
            with_momentum = (momentum == "up") == (r["direction"] == "UP")
            cells.setdefault(("direction_vs_momentum", "with" if with_momentum else "against"), []).append(r["correct"])
    tested = {k: v for k, v in cells.items() if len(v) >= MIN_PATTERN_CALLS and k[1] != "unknown"}
    pvals = {k: _binom_two_sided(sum(v), len(v), p0) for k, v in tested.items()}
    adjusted = holm(pvals) if pvals else {}
    for k, v in tested.items():
        acc = sum(v) / len(v)
        if adjusted.get(k, 1.0) < ALPHA:
            low, high = wilson_interval(sum(v), len(v))
            report["patterns"].append({
                "condition": k[0], "value": k[1], "calls": len(v), "accuracy": round(acc, 4),
                "accuracy_wilson_95": [round(low, 4), round(high, 4)], "overall_accuracy": round(rate, 4),
                "holm_adjusted_p": round(adjusted[k], 6)})
    report["status"] = "MEASURED"
    report["cells_tested"] = len(tested)
    return report


def promotion_decision(candidate, current):
    """candidate/current: {'correct', 'calls', 'markets', 'log_loss'} on FRESH (unseen) markets.

    Never applies anything. PROMOTE_ELIGIBLE means a human may review and apply it."""
    reasons = []
    fresh = candidate.get("markets") or 0
    if fresh < MIN_FRESH_CONTRACTS:
        reasons.append(f"only {fresh} fresh markets (need {MIN_FRESH_CONTRACTS})")
    cur_acc = (current["correct"] / current["calls"]) if current.get("calls") else None
    cand_low = wilson_interval(candidate["correct"], candidate["calls"])[0] if candidate.get("calls") else None
    if cur_acc is None or cand_low is None:
        reasons.append("no displayed calls to compare")
    elif cand_low <= cur_acc:
        reasons.append(f"candidate 95% lower bound {cand_low:.3f} does not exceed current accuracy {cur_acc:.3f}")
    if candidate.get("log_loss") is None or current.get("log_loss") is None or candidate["log_loss"] >= current["log_loss"]:
        reasons.append("candidate log loss is not lower than the current model's")
    decision = "HOLD" if reasons else "PROMOTE_ELIGIBLE"
    return {"decision": decision, "reasons": reasons or ["meets every rule on unseen markets"],
            "applied": False, "rollback": "the current live model is unchanged; keep its artifact as the rollback reference",
            "fresh_markets": fresh}


def write_status(path, status):
    """Atomic, private write so the live server never reads a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".learning-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(status, f, indent=1, sort_keys=True, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_status(path):
    """Read-only for the live dashboard. Never invents results.

    missing or unset path -> NOT_RUN; present but not valid JSON or not an object ->
    UNREADABLE (a real problem to investigate); valid -> the published status."""
    if not path:
        return {"status": "NOT_RUN", "format": FORMAT,
                "note": "No learning run has been published yet. The live model is unchanged."}
    try:
        text = Path(path).read_text()
    except OSError:
        return {"status": "NOT_RUN", "format": FORMAT,
                "note": "No learning run has been published yet. The live model is unchanged."}
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return {"status": "UNREADABLE", "format": FORMAT,
                "note": "The published learning status is unreadable and was ignored. The live model is unchanged."}
    return value


def summarize(ledger, patterns, promotion, generated_at):
    displayed = [r for r in ledger if r["displayed"]]
    scored = [r for r in displayed if r["correct"] is not None]
    skipped = [r for r in ledger if not r["displayed"]]
    return {
        "format": FORMAT, "status": "MEASURED", "generated_at": generated_at,
        "ledger": {"openings_recorded": len(ledger), "displayed_calls": len(displayed),
                   "skipped_openings": len(skipped), "scored_displayed_calls": len(scored),
                   "correct": sum(1 for r in scored if r["correct"]),
                   "accuracy": (sum(1 for r in scored if r["correct"]) / len(scored)) if scored else None},
        "failure_patterns": patterns, "promotion": promotion,
        "live_model": "UNCHANGED (learning never replaces it automatically)",
    }

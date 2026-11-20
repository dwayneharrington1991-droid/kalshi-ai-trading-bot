"""Read-only accuracy health monitor (development diagnostic; not wired into live signals).

Separates two questions that are easy to confuse:

  technical data health  -- are the feeds, candles, inputs and opening records fresh and complete?
                            HEALTHY / DEGRADED / STALE
  predictive edge        -- do recent settled openings show the live model beating simple baselines?
                            EDGE SUPPORTED / NO VERIFIED EDGE / DEGRADED / INSUFFICIENT EVIDENCE

A healthy feed never implies an edge. The journal is opened with SQLite mode=ro and
PRAGMA query_only; nothing is written. The report recommends WAIT when data are unreliable and
NO VERIFIED EDGE when the evidence does not support the calls; it never changes a live decision.
"""
import json
import math
import random
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

MISSING_HEAVY = 10          # reported share of recent openings missing at least this many inputs
MISSING_DEGRADED = 31       # above the 28 stream-dependent micro/spot inputs, which are often legitimately
                            # absent (2026-11-20 audit: their absence was not associated with worse skill)
MIN_EVIDENCE = 50           # settled live-model openings needed before judging the edge
WINDOW = 100                # most recent settled live-model openings scored
FEE_RATE = 0.07
DISPLAYABLE_CONFIDENCE = {"CALIBRATED", "EXPERIMENTAL CALIBRATED CANDIDATE"}   # same rule as the scoreboard
CACHE_SECONDS = 60


def _ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _bootstrap(values, seed=17, n=2000):
    rng = random.Random(seed)
    values = list(values)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(n))
    return [means[int(0.025 * n)], means[int(0.975 * n)]]


def _wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [centre - half, centre + half]


def _calibration_slope(ps, ys, iters=50):
    """Slope of a logistic regression of the outcome on logit(p); 1 = calibrated, >1 underconfident."""
    w0 = w1 = 0.0
    xs = [math.log(min(max(p, 1e-6), 1 - 1e-6) / (1 - min(max(p, 1e-6), 1 - 1e-6))) for p in ps]
    for _ in range(iters):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-max(-30, min(30, w0 + w1 * x))))
            g0 += q - y
            g1 += (q - y) * x
            v = q * (1 - q)
            h00 += v
            h01 += v * x
            h11 += v * x * x
        det = h00 * h11 - h01 * h01 + 1e-12
        d0 = (h11 * g0 - h01 * g1) / det
        d1 = (h00 * g1 - h01 * g0) / det
        w0 -= d0
        w1 -= d1
        if abs(d0) + abs(d1) < 1e-9:
            break
    return w1


def _technical(db, now, input_names):
    out = {"reasons": []}
    pred = db.execute("SELECT recorded_at FROM btc_engine_predictions ORDER BY rowid DESC LIMIT 400").fetchall()
    if pred:
        times = sorted(_ts(r[0]).timestamp() for r in pred)
        out["prediction_age_seconds"] = round(now.timestamp() - times[-1], 2)
        recent = [b - a for a, b in zip(times, times[1:]) if b - a <= 60]   # contract changeovers excluded
        recent.sort()
        out["prediction_cadence_p95_seconds"] = round(recent[int(0.95 * (len(recent) - 1))], 2) if recent else None
    else:
        out["prediction_age_seconds"] = None
    rows = db.execute(
        "SELECT ticker, generated_at, up, payload FROM btc_engine_opening "
        "WHERE mode='PUBLIC MARKET DATA' ORDER BY generated_at DESC LIMIT 16"
    ).fetchall()
    openings = []
    for ticker, generated, up, payload in rows:
        p = json.loads(payload) if payload else {}
        values = (p.get("frozen_features") or {}).get("values") or {}
        names = input_names or list(values)
        missing = sum(1 for n in names if not isinstance(values.get(n), (int, float))) if values else None
        openings.append({"ticker": ticker, "opens_at": p.get("opens_at"), "probability": up is not None,
                         "delay_seconds": p.get("opening_delay_seconds"), "missing_inputs": missing,
                         "data_quality": (p.get("data_quality") or {}).get("status"),
                         "reference_age_at_open_seconds": (p.get("data_quality") or {}).get("reference_age_at_open_seconds"),
                         "reference_source": p.get("reference_source")})
    out["recent_openings"] = openings[:4]
    delays = sorted(o["delay_seconds"] for o in openings if isinstance(o["delay_seconds"], (int, float)))
    out["opening_delay_median_seconds"] = round(delays[len(delays) // 2], 2) if delays else None
    sources = [o["reference_source"] for o in openings if o["reference_source"]]
    out["reference_source_switches_recent"] = sum(a != b for a, b in zip(sources, sources[1:]))
    if rows:
        latest_payload = json.loads(rows[0][3]) if rows[0][3] else {}
        out["latest_closed_history_bars"] = (latest_payload.get("data_quality") or {}).get("closed_history_bars")
    latest = openings[0] if openings else None
    out["latest_opening"] = latest
    heavy = [o for o in openings if o["missing_inputs"] is not None and o["missing_inputs"] >= MISSING_HEAVY]
    out["share_recent_openings_missing_10_plus_inputs"] = round(len(heavy) / len(openings), 3) if openings else None
    status = "HEALTHY"
    age = out.get("prediction_age_seconds")
    if age is None or age > 30:
        status = "STALE"
        out["reasons"].append("no live prediction recorded in the last 30 s")
    if latest is None or now.timestamp() - _ts(latest["opens_at"]).timestamp() > 16 * 60:
        status = "STALE"
        out["reasons"].append("no opening record for the current quarter hour")
    if status != "STALE":
        cadence = out.get("prediction_cadence_p95_seconds")
        if cadence is not None and cadence > 10:
            status = "DEGRADED"
            out["reasons"].append(f"prediction cadence p95 {cadence} s > 10 s")
        if latest and not latest["probability"]:
            status = "DEGRADED"
            out["reasons"].append("latest opening withheld (no probability)")
        if latest and latest["missing_inputs"] is not None and latest["missing_inputs"] >= MISSING_DEGRADED:
            status = "DEGRADED"
            out["reasons"].append(f"latest opening missing {latest['missing_inputs']} model inputs")
        if latest and (latest["delay_seconds"] or 0) > 10:
            status = "DEGRADED"
            out["reasons"].append(f"latest opening generated {latest['delay_seconds']:.1f} s after the open")
    out["status"] = status
    return out


def _scoreboards(db):
    """Both call populations, with every exclusion counted. Nothing is hidden or removed."""
    rows = db.execute(
        """SELECT o.ticker, o.up, o.payload, f.outcome FROM btc_engine_opening o
        LEFT JOIN forecast_outcomes f ON f.ticker=o.ticker AND f.origin='official'
        WHERE o.mode='PUBLIC MARKET DATA' ORDER BY o.generated_at"""
    ).fetchall()
    dash = {"calls": 0, "correct": 0}
    every = {"calls": 0, "correct": 0}
    excluded = {}
    for _ticker, up, payload, outcome in rows:
        p = json.loads(payload) if payload else {}
        direction = p.get("direction")
        if direction not in ("UP", "DOWN") or outcome not in (0, 1):
            continue
        right = (direction == "UP") == (outcome == 1)
        every["calls"] += 1
        every["correct"] += right
        status = (p.get("confidence") or {}).get("status")
        if status in DISPLAYABLE_CONFIDENCE and isinstance(up, (int, float)) and (direction == "UP") == (up >= 0.5):
            dash["calls"] += 1
            dash["correct"] += right
        else:
            reason = f"confidence status {status!r} not displayable ({p.get('model') or 'unrecorded model'})"
            entry = excluded.setdefault(reason, {"calls": 0, "correct": 0})
            entry["calls"] += 1
            entry["correct"] += right
    for d in (dash, every):
        d["accuracy"] = d["correct"] / d["calls"] if d["calls"] else None
        d["accuracy_ci95"] = _wilson(d["correct"], d["calls"])
    return {"dashboard_scoreboard": dash, "all_directional_calls": every,
            "excluded_from_dashboard_scoreboard": excluded,
            "note": "The dashboard scoreboard counts only calls whose confidence was displayable; the other "
                    "directional records are listed here with their results, never removed."}


def _input_drift(db, artifact, n=50):
    """Mean standardized shift of recent present inputs from the model's training statistics."""
    try:
        prep = artifact["model"]["components"][0]["preprocessor"]
        names, means, scales = prep["names"], prep["means"], prep["scales"]
    except (KeyError, IndexError, TypeError):
        return None
    rows = db.execute("SELECT payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA' "
                      "ORDER BY generated_at DESC LIMIT ?", (n,)).fetchall()
    sums = {}
    for (payload,) in rows:
        values = ((json.loads(payload) if payload else {}).get("frozen_features") or {}).get("values") or {}
        for name, mean, scale in zip(names, means, scales):
            v = values.get(name)
            if isinstance(v, (int, float)) and math.isfinite(v) and scale:
                total, count = sums.get(name, (0.0, 0))
                sums[name] = (total + (v - mean) / scale, count + 1)
    shifts = {name: abs(t / c) for name, (t, c) in sums.items() if c >= 10}
    if not shifts:
        return None
    top = sorted(shifts.items(), key=lambda kv: -kv[1])[:5]
    return {"openings": len(rows), "inputs_compared": len(shifts),
            "mean_abs_standardized_shift": sum(shifts.values()) / len(shifts),
            "largest": [{"input": k, "shift_sd": round(v, 2)} for k, v in top],
            "status": "DRIFTED" if sum(shifts.values()) / len(shifts) > 0.5 else "WITHIN TRAINING RANGE"}


def _predictive(db, artifact_model_id=None):
    rows = db.execute(
        """SELECT o.ticker, o.up, o.payload, f.outcome FROM btc_engine_opening o
        JOIN forecast_outcomes f ON f.ticker=o.ticker AND f.origin='official'
        WHERE o.mode='PUBLIC MARKET DATA' AND o.up IS NOT NULL ORDER BY o.generated_at"""
    ).fetchall()
    quotes = {}
    for ticker, sec, ua, da in db.execute(
            "SELECT ticker, seconds_from_open, up_ask, down_ask FROM opening_quote_snapshots "
            "WHERE seconds_from_open BETWEEN -5 AND 30 AND up_ask IS NOT NULL AND down_ask IS NOT NULL"):
        if ticker not in quotes or abs(sec) < abs(quotes[ticker][0]):
            quotes[ticker] = (sec, ua, da)
    history, scored = [], []
    model_name = None
    for ticker, up, payload, outcome in rows:
        p = json.loads(payload) if payload else {}
        base = (sum(history) + 1) / (len(history) + 2)          # past-only base rate
        history.append(int(outcome))
        model = p.get("model") or ""
        if artifact_model_id and artifact_model_id not in model:
            continue
        if not model.startswith("opening-candidate"):
            continue
        model_name = model
        scored.append({"ticker": ticker, "p": float(up), "y": int(outcome), "base": base,
                       "direction": p.get("direction"), "quote": quotes.get(ticker)})
    window = scored[-WINDOW:]
    out = {"model": model_name, "settled_live_model_openings": len(scored), "window": len(window)}
    if len(window) < MIN_EVIDENCE:
        out["status"] = "INSUFFICIENT EVIDENCE"
        return out
    skill = [(r["base"] - r["y"]) ** 2 - (r["p"] - r["y"]) ** 2 for r in window]
    out["brier_model"] = sum((r["p"] - r["y"]) ** 2 for r in window) / len(window)
    out["brier_past_base_rate"] = sum((r["base"] - r["y"]) ** 2 for r in window) / len(window)
    out["skill_vs_base_ci95"] = _bootstrap(skill)
    with_q = [r for r in window if r["quote"]]
    if len(with_q) >= MIN_EVIDENCE:
        mids = [((r["quote"][1] + 1 - r["quote"][2]) / 2) for r in with_q]   # ask-implied mid: (up_ask + 1 - down_ask)/2
        mskill = [(m - r["y"]) ** 2 - (r["p"] - r["y"]) ** 2 for m, r in zip(mids, with_q)]
        out["skill_vs_market_ci95"] = _bootstrap(mskill, seed=19)
    calls = [r for r in window if r["direction"] in ("UP", "DOWN")]
    correct = sum((r["direction"] == "UP") == (r["y"] == 1) for r in calls)
    out["calls"] = len(calls)
    out["coverage"] = len(calls) / len(window)
    out["call_accuracy"] = correct / len(calls) if calls else None
    out["call_accuracy_ci95"] = _wilson(correct, len(calls))
    pnl = []
    for r in calls:
        if not r["quote"]:
            continue
        ask = r["quote"][1] if r["direction"] == "UP" else r["quote"][2]
        if 0 < ask < 1:
            fee = math.ceil(100 * FEE_RATE * ask * (1 - ask)) / 100
            pnl.append((1 - ask if (r["direction"] == "UP") == (r["y"] == 1) else -ask) - fee)
    out["after_cost_pnl_per_contract"] = sum(pnl) / len(pnl) if pnl else None
    out["after_cost_pnl_ci95"] = _bootstrap(pnl, seed=23) if len(pnl) >= 20 else None
    out["calibration_slope"] = _calibration_slope([r["p"] for r in window], [r["y"] for r in window])
    lo, hi = out["skill_vs_base_ci95"]
    market = out.get("skill_vs_market_ci95")
    if hi < 0:
        out["status"] = "DEGRADED"
        out["reason"] = "recent probabilities are reliably worse than the past-only base rate"
    elif lo > 0 and (market is None or market[0] > 0) and out["after_cost_pnl_ci95"] and out["after_cost_pnl_ci95"][0] > 0:
        out["status"] = "EDGE SUPPORTED"
        out["reason"] = "beats the base rate and the market price, with positive after-cost P&L, in the recent window"
    else:
        out["status"] = "NO VERIFIED EDGE"
        out["reason"] = "recent results do not reliably beat the base rate, the market price and costs"
    return out


def assess(journal, *, now=None, artifact=None):
    """Read-only report. `artifact` (the live opening artifact dict) is optional; it names the inputs and model."""
    now = now or datetime.now(timezone.utc)
    names, model_id, model_age = None, None, None
    if artifact:
        names = artifact["model"]["components"][0]["preprocessor"]["names"]
        model_id = artifact.get("model_id")
        model_age = (now - _ts(artifact["labels_available_through"])).total_seconds() / 86400
    with closing(sqlite3.connect(f"file:{journal}?mode=ro", uri=True, timeout=30)) as db:
        db.execute("PRAGMA query_only=ON")
        technical = _technical(db, now, names)
        predictive = _predictive(db, model_id)
        scoreboards = _scoreboards(db)
        drift = _input_drift(db, artifact) if artifact else None
    predictive["model_id"] = model_id
    predictive["input_drift"] = drift
    predictive["training_labels_age_days"] = round(model_age, 2) if model_age is not None else None
    if technical["status"] != "HEALTHY":
        recommendation = "WAIT: data are " + technical["status"].lower() + " (" + "; ".join(technical["reasons"]) + ")"
    elif predictive["status"] != "EDGE SUPPORTED":
        recommendation = "NO VERIFIED EDGE: treat UP/DOWN calls as unproven"
    else:
        recommendation = "Data healthy and recent edge supported (not a guarantee of future results)"
    return {"generated_at": now.isoformat(), "technical_data_health": technical,
            "predictive_edge": predictive, "scoreboards": scoreboards, "recommendation": recommendation,
            "note": "Diagnostic only; it never changes a live signal, threshold, model or alert."}


class CachedReport:
    """At most one assessment per CACHE_SECONDS for the dashboard endpoint (read-only, bounded cost)."""

    def __init__(self, journal, artifact=None, clock=lambda: datetime.now(timezone.utc)):
        self.journal, self.artifact, self.clock = journal, artifact, clock
        self.value, self.at = None, None

    def get(self):
        now = self.clock()
        if self.value is None or (now - self.at).total_seconds() >= CACHE_SECONDS:
            try:
                self.value = assess(self.journal, now=now, artifact=self.artifact)
            except Exception as exc:                      # a diagnostic failure never affects live signals
                self.value = {"generated_at": now.isoformat(), "status": "UNAVAILABLE",
                              "reason": type(exc).__name__}
            self.at = now
        return self.value


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--journal", required=True)
    ap.add_argument("--artifact")
    args = ap.parse_args(argv)
    artifact = json.load(open(args.artifact)) if args.artifact else None
    print(json.dumps(assess(args.journal, artifact=artifact), indent=1, default=str))


if __name__ == "__main__":
    main()

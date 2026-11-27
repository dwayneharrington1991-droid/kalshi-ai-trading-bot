"""Read-only seven-day test report from the alert service's immutable records (research/reporting only).

Uses scoreboard7.compute for the official window numbers and adds, per system: pending (locked but not yet
settled), average stated confidence, last locked prediction, last-24-hour slice, and a reconciliation of the
dashboard scoreboard (COMBINED CALL) with the backend.

  python3 scripts/seven_day_report.py [--db ~/dual-ai-alerts/sent.sqlite3]
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import scoreboard7 as sb


def conf(direction, p_up):
    if direction == "UP":
        return p_up
    if direction == "DOWN":
        return 1 - p_up
    return None


def report(db, window, now=None):
    now = now or datetime.now(timezone.utc)
    start, end = sb._ts(window["start"]), sb._ts(window["end"])
    full = sb.compute(db, window, now)
    last24 = sb.compute(db, {**window, "start": max(start, now - timedelta(hours=24)).isoformat()}, now)
    settled = {t for (t,) in db.execute("SELECT ticker FROM outcomes7")}
    extra = {m: {"locked": 0, "pending": 0, "confidences": [], "last": None} for m in sb.MODELS}
    for t, gen, kind, live, lu, ch, cu, comb, ku in db.execute(
            "SELECT ticker, generated_at, kind, live, live_up, challenger, challenger_up, combined, combined_up FROM openings"):
        g = sb._ts(gen)
        if not start <= g < end:
            continue
        for m, d, p in (("AI #1", live, lu), ("AI #2", ch, cu), ("COMBINED CALL", comb, ku)):
            if d is None:
                continue
            e = extra[m]; e["locked"] += 1; e["pending"] += t not in settled
            c = conf(d, p) if isinstance(p, (int, float)) else None
            if c is not None:
                e["confidences"].append(c)
            if e["last"] is None or gen > e["last"]["at"]:
                e["last"] = {"ticker": t, "at": gen, "call": d, "p_up": p}
    for t, lk, d, payload in db.execute("SELECT ticker, locked_at, direction, payload FROM minute6"):
        if not start <= sb._ts(lk) < end:
            continue
        e = extra["MINUTE-6 FINAL"]; e["locked"] += 1; e["pending"] += t not in settled
        p = (json.loads(payload or "{}") or {}).get("consensus_p_up_average")
        c = conf(d, p) if isinstance(p, (int, float)) else None
        if c is not None:
            e["confidences"].append(c)
        if e["last"] is None or lk > e["last"]["at"]:
            e["last"] = {"ticker": t, "at": lk, "call": d}
    try:
        f6_done = {t for (t,) in db.execute("SELECT ticker FROM first6_outcomes")}
        for t, lk, d, p in db.execute("SELECT ticker, locked_at, direction, p_up FROM first6"):
            if not start <= sb._ts(lk) < end:
                continue
            e = extra["FIRST 6 MIN"]; e["locked"] += 1; e["pending"] += t not in f6_done
            e["confidences"].append(max(p, 1 - p))
            if e["last"] is None or lk > e["last"]["at"]:
                e["last"] = {"ticker": t, "at": lk, "call": d, "p_up": p}
    except sqlite3.OperationalError:
        pass
    models = {}
    for m in sb.MODELS:
        c, e, l24 = full["models"][m], extra[m], last24["models"][m]
        n = c["correct"] + c["wrong"]
        models[m] = {"right": c["correct"], "wrong": c["wrong"], "directional": n,
                     "accuracy": f"{c['correct']}/{n} = {c['accuracy']}%" if n else "N/A (no settled directional calls)",
                     "wait": c["wait"], "missing": c["missing"], "eligible_settled": c["eligible"], "coverage_pct": c["coverage"],
                     "locked_in_window": e["locked"], "pending_outcome": e["pending"],
                     "avg_stated_confidence": round(sum(e["confidences"]) / len(e["confidences"]), 3) if e["confidences"] else None,
                     "last_prediction": e["last"],
                     "last_24h": {"right": l24["correct"], "wrong": l24["wrong"], "wait": l24["wait"], "accuracy": l24["accuracy"]}}
    return {"window": full["window"], "settled_markets": full["settled_markets"],
            "first6_scoring_start": full["first6_scoring_start"], "models": models,
            "targets": "COMBINED CALL / AI #1 / AI #2 / MINUTE-6 FINAL vs official Kalshi settlement; FIRST 6 MIN vs BTC open->minute 6",
            "generated_at": now.isoformat()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(Path.home() / "dual-ai-alerts" / "sent.sqlite3"))
    a = ap.parse_args(argv)
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    window = json.loads(Path(a.db).with_name("score_window.json").read_text())
    print(json.dumps(report(db, window), indent=1, default=str))


if __name__ == "__main__":
    main()

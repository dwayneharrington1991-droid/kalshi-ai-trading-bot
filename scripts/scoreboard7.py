"""Seven-day scoring window for the four visible predictions (display/scoring window only; nothing is deleted).

The window (start, end = start + 168 h) is written ONCE to score_window.json and never overwritten, so it
survives restarts and deployments and its final results stay readable after it ends. Scored per model:
  * eligible market = a KXBTC15M market that opens inside [start, end) and has an official settlement;
  * a prediction counts only if it was locked at or after `start` and before `end` (openings: generated_at;
    minute-6: locked_at), one per market, never replaced by a later reading;
  * CORRECT / WRONG for UP/DOWN against the official outcome, WAIT for a genuine WAIT, MISSING when the model
    has no valid locked reading for that market (never silently counted as WAIT);
  * accuracy = CORRECT / (CORRECT + WRONG), N/A until one directional call has settled.
Opening calls (COMBINED, AI #1, AI #2) and MINUTE-6 are scored separately. All-time records are untouched.

  python3 scoreboard7.py start --db sent.sqlite3 [--window score_window.json]   # starts the test once
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOURS = 168
MODELS = ("COMBINED", "AI #1", "AI #2", "MINUTE-6")


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def start_window(path, now=None, hours=HOURS, model=None):
    """Write the window once. Returns the stored window (an existing one is never changed)."""
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text())
    now = now or datetime.now(timezone.utc)
    w = {"format": "kxbtc-7day-window-v1", "start": now.isoformat(), "end": (now + timedelta(hours=hours)).isoformat(),
         "hours": hours, "created_at": now.isoformat(), "production_model_at_start": model,
         "scope": "Display/scoring window only. Historical predictions, outcomes and all-time scoreboards are untouched."}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(w, indent=1))
    os.replace(tmp, path)
    return w


def ensure_tables(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS outcomes7(ticker TEXT PRIMARY KEY, opens_at TEXT, outcome TEXT, seen_at TEXT);
        CREATE TRIGGER IF NOT EXISTS outcomes7_immutable BEFORE UPDATE ON outcomes7
            BEGIN SELECT RAISE(ABORT, 'official outcomes are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS outcomes7_no_delete BEFORE DELETE ON outcomes7
            BEGIN SELECT RAISE(ABORT, 'official outcomes are immutable'); END;
        CREATE TABLE IF NOT EXISTS opening_models(ticker TEXT PRIMARY KEY, model TEXT, recorded_at TEXT);""")
    db.commit()


def record_outcomes(db, markets, now=None):
    """Keep official settlements (the API pages only recent markets). Only VERIFIED_OFFICIAL UP/DOWN."""
    now = (now or datetime.now(timezone.utc)).isoformat()
    n = 0
    for m in markets or []:
        if m.get("outcome_status") == "VERIFIED_OFFICIAL" and m.get("final_outcome") in ("UP", "DOWN") and m.get("opening_at"):
            n += db.execute("INSERT OR IGNORE INTO outcomes7 VALUES (?,?,?,?)",
                            (m["ticker"], m["opening_at"], m["final_outcome"], now)).rowcount
    db.commit()
    return n


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def compute(db, window, now=None):
    now = now or datetime.now(timezone.utc)
    start, end = _ts(window["start"]), _ts(window["end"])
    board = {m: {"correct": 0, "wrong": 0, "wait": 0, "missing": 0} for m in MODELS}
    markets = [(t, o) for t, opens, o in db.execute("SELECT ticker, opens_at, outcome FROM outcomes7")
               if start <= _ts(opens) < end]
    opens = {}
    if _table(db, "openings"):
        for t, gen, kind, live, ch, comb in db.execute(
                "SELECT ticker, generated_at, kind, live, challenger, combined FROM openings"):
            if start <= _ts(gen) < end:
                ready = kind == "READY"
                opens[t] = {"AI #1": live, "AI #2": ch if ready else None, "COMBINED": comb if ready else None}
    m6 = {}
    if _table(db, "minute6"):
        for t, locked, direction, payload in db.execute("SELECT ticker, locked_at, direction, payload FROM minute6"):
            if start <= _ts(locked) < end:
                ok = (json.loads(payload or "{}").get("eligible") or 0) >= 2
                m6[t] = direction if ok else None
    for t, outcome in markets:
        calls = dict(opens.get(t, {}))
        calls["MINUTE-6"] = m6.get(t)
        for m in MODELS:
            d, cell = calls.get(m), board[m]
            if d in ("UP", "DOWN"):
                cell["correct" if d == outcome else "wrong"] += 1
            elif d == "WAIT":
                cell["wait"] += 1
            else:
                cell["missing"] += 1
    for cell in board.values():
        n = cell["correct"] + cell["wrong"]
        cell["accuracy"] = round(100 * cell["correct"] / n, 1) if n else None
    elapsed = min(max((now - start).total_seconds() / 3600, 0), window["hours"])
    return {"window": {"start": window["start"], "end": window["end"], "hours": window["hours"],
                       "elapsed_hours": round(elapsed, 2), "complete": now >= end,
                       "production_model_at_start": window.get("production_model_at_start")},
            "settled_markets": len(markets), "models": board,
            "rule": "accuracy = correct / (correct + wrong); WAIT and MISSING not counted; one locked prediction per market"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Seven-day scoring window")
    ap.add_argument("cmd", choices=["start", "show"])
    ap.add_argument("--db", required=True)
    ap.add_argument("--window", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args(argv)
    path = a.window or str(Path(a.db).with_name("score_window.json"))
    if a.cmd == "start":
        print(json.dumps(start_window(path, model=a.model), indent=1))
    else:
        db = sqlite3.connect(a.db)
        ensure_tables(db)
        print(json.dumps(compute(db, json.loads(Path(path).read_text())), indent=1))


if __name__ == "__main__":
    main()

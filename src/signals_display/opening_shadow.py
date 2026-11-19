"""Independent shadow-only scoring of the 87-input logistic opening challenger.

Shadow only: it never changes live predictions, the dashboard, Telegram, the learning pipeline, automatic promotion,
the qualification gates or any trading. It reads the live journal read-only, refits its own model at most once a day
from outcomes already observed, scores each newly recorded opening once from its frozen inputs, and appends the result
to its own ledger. The ledger is append-only and keyed by ticker, so duplicates and retrospective edits are impossible.
"""
import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import opening_challengers as oc
from . import opening_research as research

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_openings (
    ticker TEXT PRIMARY KEY,
    opens_at TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    seconds_after_open REAL NOT NULL,
    inputs_complete INTEGER NOT NULL,
    probability REAL,
    decision TEXT NOT NULL,
    model_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS shadow_no_update BEFORE UPDATE ON shadow_openings
BEGIN SELECT RAISE(ABORT, 'shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_no_delete BEFORE DELETE ON shadow_openings
BEGIN SELECT RAISE(ABORT, 'shadow ledger is append-only'); END;
"""

REFIT_SECONDS = 24 * 3600


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def serialize_model(fitted, trained_at, training_rows):
    w, b = fitted["model"]
    body = {"format": "opening-shadow-logistic-v1", "trained_at": trained_at.isoformat(),
            "training_rows": training_rows, "names": fitted["names"], "means": fitted["means"],
            "scales": fitted["scales"], "weights": w, "bias": b, "platt": list(fitted["platt"]),
            "l2": fitted["l2"]}
    body["sha256"] = _sha(json.dumps({k: v for k, v in body.items() if k != "sha256"}, sort_keys=True).encode())
    return body


def load_model(payload):
    fitted = {"model": (payload["weights"], payload["bias"]), "means": payload["means"],
              "scales": payload["scales"], "names": payload["names"], "platt": tuple(payload["platt"]),
              "l2": payload["l2"]}
    return fitted


def model_is_fresh(payload, now):
    return payload is not None and now - _ts(payload["trained_at"]) < timedelta(seconds=REFIT_SECONDS)


def scoring_row(values):
    return {"features": values}


def score_openings(candidates, fitted, model_sha):
    """Score each candidate once. candidates: [(ticker, opens_at_iso, scored_at_iso, values_or_None)]."""
    out = []
    for ticker, opens_at, scored_at, values in candidates:
        seconds = (_ts(scored_at) - _ts(opens_at)).total_seconds()
        complete = values is not None and all(
            n in values and isinstance(values[n], (int, float)) and abs(values[n]) < 1e300 for n in fitted["names"])
        if complete:
            p = oc.predict_logistic(fitted, [scoring_row(values)], calibrated=True)[0]
            decision = oc.call_of(p)
            probability = round(p, 6)
        else:
            probability, decision = None, "NOT_SCORED"
        out.append({"ticker": ticker, "opens_at": opens_at, "scored_at": scored_at,
                    "seconds_after_open": round(seconds, 3), "inputs_complete": 1 if complete else 0,
                    "probability": probability, "decision": decision, "model_sha256": model_sha})
    return out


def write_ledger(shadow_db, rows):
    written = 0
    with sqlite3.connect(shadow_db) as db:
        db.executescript(SCHEMA)
        for r in rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO shadow_openings VALUES (?,?,?,?,?,?,?,?)",
                (r["ticker"], r["opens_at"], r["scored_at"], r["seconds_after_open"], r["inputs_complete"],
                 r["probability"], r["decision"], r["model_sha256"]))
            written += cur.rowcount
    return written


def unscored_openings(journal_ro, shadow_db, names, now, limit=500):
    """Newly recorded openings not yet in the ledger. Values come only from the frozen opening payload."""
    seen = set()
    if Path(shadow_db).exists():
        with sqlite3.connect(shadow_db) as led:
            try:
                seen = {t for (t,) in led.execute("SELECT ticker FROM shadow_openings")}
            except sqlite3.OperationalError:
                seen = set()
    rows = journal_ro.execute(
        "SELECT ticker, generated_at, payload FROM btc_engine_opening WHERE mode='PUBLIC MARKET DATA' "
        "AND generated_at <= ? ORDER BY generated_at DESC LIMIT ?", (now.isoformat(), limit)).fetchall()
    out = []
    for ticker, generated_at, payload in rows:
        if ticker in seen:
            continue
        try:
            opening = json.loads(payload)
            opens_at = opening["opens_at"]
            values = opening["frozen_features"]["values"]
        except (TypeError, ValueError, KeyError):
            opens_at, values = generated_at, None
        out.append((ticker, opens_at, now.isoformat(), values))
    return out


def run(journal, shadow_dir, artifact, now, dry_run=False):
    shadow_dir = Path(shadow_dir)
    shadow_dir.mkdir(parents=True, exist_ok=True)
    names = oc.production_inputs(json.loads(Path(artifact).read_text()))
    model_path = shadow_dir / "shadow_model.json"
    payload = json.loads(model_path.read_text()) if model_path.exists() else None
    with sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30) as ro:
        ro.execute("PRAGMA query_only=ON")
        if not model_is_fresh(payload, now):
            rows = research.load(ro, now)
            fitted = oc.fit_logistic(rows, names)
            payload = serialize_model(fitted, now, len(rows))
            if not dry_run:
                tmp = model_path.with_suffix(".json.next")
                tmp.write_text(json.dumps(payload, sort_keys=True))
                os.chmod(tmp, 0o600)
                os.replace(tmp, model_path)
        fitted = load_model(payload)
        candidates = unscored_openings(ro, shadow_dir / "shadow_ledger.sqlite3", fitted["names"], now)
    rows = score_openings(candidates, fitted, payload["sha256"])
    written = 0 if dry_run else write_ledger(shadow_dir / "shadow_ledger.sqlite3", rows)
    return {"model_sha256": payload["sha256"], "training_rows": payload["training_rows"],
            "input_names": len(fitted["names"]), "new_openings": len(rows), "written": written,
            "decisions": {d: sum(1 for r in rows if r["decision"] == d) for d in ("UP", "DOWN", "WAIT", "NOT_SCORED")}}


def _main(argv=None):
    ap = argparse.ArgumentParser(description="independent shadow-only opening challenger")
    ap.add_argument("--journal", type=Path, required=True)
    ap.add_argument("--shadow-dir", type=Path, required=True)
    ap.add_argument("--artifact", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    result = run(args.journal, args.shadow_dir, args.artifact, datetime.now(timezone.utc), args.dry_run)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    _main()

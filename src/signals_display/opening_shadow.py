"""Independent shadow-only scoring of the 87-input logistic opening challenger.

Shadow only: it never changes live predictions, the dashboard, Telegram, the learning pipeline, automatic promotion,
the qualification gates or trading. Guarantees:

* Genesis: only openings generated after the shadow's first run are scored. Earlier openings are never scored, so a
  replay of history can never count as a prospective prediction.
* Unseen outcome: an opening is scored only if no official outcome exists for it yet at scoring time.
* Versioned models: each refit is a write-once version recording its training cutoff. An opening is scored only with
  the newest version trained before the opening was generated, so no prediction uses labels from its own future.
* Refit window: refits happen only in the configured UTC hour, so they do not compete with the learning cycle.
* Append-only ledger keyed by ticker: duplicates and retrospective edits are impossible.
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
    generated_at TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS shadow_outcomes (
    ticker TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    outcome INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS shadow_outcome_no_update BEFORE UPDATE ON shadow_outcomes
BEGIN SELECT RAISE(ABORT, 'shadow outcomes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS shadow_outcome_no_delete BEFORE DELETE ON shadow_outcomes
BEGIN SELECT RAISE(ABORT, 'shadow outcomes are append-only'); END;
"""

REFIT_SECONDS = 24 * 3600
REFIT_HOUR_UTC = 3


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def serialize_model(fitted, trained_at, training_rows, training_cutoff):
    w, b = fitted["model"]
    body = {"format": "opening-shadow-logistic-v2", "trained_at": trained_at.isoformat(),
            "training_cutoff": training_cutoff, "training_rows": training_rows, "names": fitted["names"],
            "means": fitted["means"], "scales": fitted["scales"], "weights": w, "bias": b,
            "platt": list(fitted["platt"]), "l2": fitted["l2"]}
    body["sha256"] = _sha(json.dumps(body, sort_keys=True).encode())
    return body


def load_model(payload):
    return {"model": (payload["weights"], payload["bias"]), "means": payload["means"], "scales": payload["scales"],
            "names": payload["names"], "platt": tuple(payload["platt"]), "l2": payload["l2"]}


def model_is_fresh(payload, now):
    return payload is not None and now - _ts(payload["trained_at"]) < timedelta(seconds=REFIT_SECONDS)


def scoring_row(values):
    return {"features": values}


def version_for(versions, generated_at):
    """Newest version trained at or before the opening was generated. None means no prospective model exists yet."""
    eligible = [v for v in versions if _ts(v["trained_at"]) <= _ts(generated_at)]
    return max(eligible, key=lambda v: _ts(v["trained_at"])) if eligible else None


def score_openings(candidates, versions, now):
    """candidates: [(ticker, opens_at, generated_at, values_or_None)]. Each is scored once, by a version trained before it."""
    out = []
    for ticker, opens_at, generated_at, values in candidates:
        version = version_for(versions, generated_at)
        seconds = (now - _ts(opens_at)).total_seconds()
        if version is None:
            probability, decision, complete, sha = None, "NOT_SCORED", 0, "NO_MODEL_BEFORE_OPENING"
        else:
            fitted = load_model(version)
            complete = values is not None and all(
                n in values and isinstance(values[n], (int, float)) and abs(values[n]) < 1e300 for n in fitted["names"])
            sha = version["sha256"]
            if complete:
                p = oc.predict_logistic(fitted, [scoring_row(values)], calibrated=True)[0]
                probability, decision = round(p, 6), oc.call_of(p)
            else:
                probability, decision = None, "NOT_SCORED"
        out.append({"ticker": ticker, "opens_at": opens_at, "generated_at": generated_at,
                    "scored_at": now.isoformat(), "seconds_after_open": round(seconds, 3),
                    "inputs_complete": 1 if complete else 0, "probability": probability,
                    "decision": decision, "model_sha256": sha})
    return out


def write_ledger(shadow_db, rows):
    written = 0
    with sqlite3.connect(shadow_db) as db:
        db.executescript(SCHEMA)
        for r in rows:
            cur = db.execute(
                "INSERT OR IGNORE INTO shadow_openings VALUES (?,?,?,?,?,?,?,?,?)",
                (r["ticker"], r["opens_at"], r["generated_at"], r["scored_at"], r["seconds_after_open"],
                 r["inputs_complete"], r["probability"], r["decision"], r["model_sha256"]))
            written += cur.rowcount
    return written


def sync_outcomes(journal_ro, shadow_db, now):
    """Copy official outcomes for tickers already in the shadow ledger. Copies only; nothing is inferred."""
    with sqlite3.connect(shadow_db) as led:
        led.executescript(SCHEMA)
        pending = [t for (t,) in led.execute(
            "SELECT ticker FROM shadow_openings WHERE ticker NOT IN (SELECT ticker FROM shadow_outcomes)")]
    found = []
    for ticker in pending:
        row = journal_ro.execute(
            "SELECT observed_at, outcome FROM forecast_outcomes WHERE ticker=? AND origin='official' "
            "AND outcome IN (0, 1)", (ticker,)).fetchone()
        if row is not None:
            found.append((ticker, row[0], int(row[1]), now.isoformat()))
    written = 0
    with sqlite3.connect(shadow_db) as led:
        for ticker, observed_at, outcome, synced in found:
            written += led.execute("INSERT OR IGNORE INTO shadow_outcomes VALUES (?,?,?,?)",
                                   (ticker, observed_at, outcome, synced)).rowcount
    return written


def genesis(shadow_dir, now, dry_run=False):
    path = Path(shadow_dir) / "genesis.json"
    if path.exists():
        return _ts(json.loads(path.read_text())["genesis_at"])
    if not dry_run:
        path.write_text(json.dumps({"genesis_at": now.isoformat()}, sort_keys=True))
        os.chmod(path, 0o600)
    return now


def load_versions(shadow_dir):
    vdir = Path(shadow_dir) / "versions"
    if not vdir.exists():
        return []
    versions = [json.loads(p.read_text()) for p in sorted(vdir.glob("*.json"))]
    return sorted(versions, key=lambda v: _ts(v["trained_at"]))


def write_version(shadow_dir, payload):
    vdir = Path(shadow_dir) / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    path = vdir / f"{payload['trained_at'].replace(':', '')}-{payload['sha256'][:12]}.json"
    if path.exists():
        raise FileExistsError("model version already recorded")
    path.write_text(json.dumps(payload, sort_keys=True))
    os.chmod(path, 0o600)
    return path


def unscored_openings(journal_ro, shadow_dir, names_present, since, now, limit=500):
    """Openings generated after genesis, with no official outcome observed yet, and not already in the ledger."""
    seen = set()
    ledger = Path(shadow_dir) / "shadow_ledger.sqlite3"
    if ledger.exists():
        with sqlite3.connect(ledger) as led:
            try:
                seen = {t for (t,) in led.execute("SELECT ticker FROM shadow_openings")}
            except sqlite3.OperationalError:
                seen = set()
    rows = journal_ro.execute(
        "SELECT o.ticker, o.generated_at, o.payload FROM btc_engine_opening o "
        "WHERE o.mode='PUBLIC MARKET DATA' AND o.generated_at > ? AND o.generated_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM forecast_outcomes f WHERE f.ticker=o.ticker AND f.origin='official') "
        "ORDER BY o.generated_at LIMIT ?", (since.isoformat(), now.isoformat(), limit)).fetchall()
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
        out.append((ticker, opens_at, generated_at, values))
    return out


def run(journal, shadow_dir, artifact, now, dry_run=False):
    shadow_dir = Path(shadow_dir)
    shadow_dir.mkdir(parents=True, exist_ok=True)
    since = genesis(shadow_dir, now, dry_run)
    names = oc.production_inputs(json.loads(Path(artifact).read_text()))
    versions = load_versions(shadow_dir)
    new_version = None
    with sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30) as ro:
        ro.execute("PRAGMA query_only=ON")
        newest = versions[-1] if versions else None
        if newest is None or (now.hour == REFIT_HOUR_UTC and not model_is_fresh(newest, now)):
            rows = research.load(ro, now)
            fitted = oc.fit_logistic(rows, names)
            cutoff = max((str(r["label_available_at"]) for r in rows), default=None)
            new_version = serialize_model(fitted, now, len(rows), cutoff)
            if not dry_run:
                write_version(shadow_dir, new_version)
            versions.append(new_version)
        candidates = unscored_openings(ro, shadow_dir, names, since, now)
    rows = score_openings(candidates, versions, now)
    written = 0 if dry_run else write_ledger(shadow_dir / "shadow_ledger.sqlite3", rows)
    synced = 0
    if not dry_run:
        with sqlite3.connect(f"file:{Path(journal).resolve()}?mode=ro", uri=True, timeout=30) as ro_outcomes:
            ro_outcomes.execute("PRAGMA query_only=ON")
            synced = sync_outcomes(ro_outcomes, shadow_dir / "shadow_ledger.sqlite3", now)
    return {"outcomes_copied": synced, "genesis_at": since.isoformat(), "versions_known": len(versions),
            "new_version": new_version["sha256"][:12] if new_version else None,
            "new_openings": len(rows), "written": written,
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

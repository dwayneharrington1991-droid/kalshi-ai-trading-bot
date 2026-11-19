"""Shadow prediction for one market's Telegram opening message. Research only.

Called only from the Telegram sender thread, never from the prediction loop. For the exact ticker in the message it
returns the immutable shadow record: the row written earlier by the scheduled scorer if one exists, otherwise a
prediction computed now from that opening's own frozen inputs with the newest model version trained before the opening
was generated, written once, append-only. It never raises and never feeds any live output, trading, alert scheduling,
learning or promotion.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import opening_challengers as oc
from . import opening_shadow as sh


def _ts(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_for(ledger, ticker):
    if not ledger.exists():
        return None
    with sqlite3.connect(f"file:{ledger.resolve()}?mode=ro", uri=True, timeout=5) as db:
        db.execute("PRAGMA query_only=ON")
        return db.execute(
            "SELECT ticker, opens_at, generated_at, scored_at, seconds_after_open, inputs_complete, probability, "
            "decision, model_sha256 FROM shadow_openings WHERE ticker=?", (ticker,)).fetchone()


def _from_row(row):
    _t, _o, _g, scored_at, seconds, complete, probability, decision, sha = row
    if decision not in ("UP", "DOWN", "WAIT") or not complete:
        return {"status": "NO PREDICTION", "reason": "no prospective prediction was recorded for this market"}
    return {"status": "RECORDED", "decision": decision, "probability": probability, "recorded_at": scored_at,
            "model": str(sha)[:12], "seconds_after_open": seconds}


def shadow_for_market(shadow_dir, *, ticker, opens_at, generated_at, frozen_values, now=None):
    """Immutable shadow record for this exact ticker. Never raises."""
    if shadow_dir is None:
        return {"status": "NOT CONFIGURED", "reason": "no shadow ledger is configured"}
    try:
        now = now or datetime.now(timezone.utc)
        ledger = Path(shadow_dir) / "shadow_ledger.sqlite3"
        existing = _row_for(ledger, ticker)
        if existing is not None:
            return _from_row(existing)                      # a recorded prediction is final; never recomputed
        version = sh.version_for(sh.load_versions(shadow_dir), generated_at)
        if version is None:
            return {"status": "PENDING", "reason": "no model version was trained before this opening yet"}
        fitted = sh.load_model(version)
        complete = frozen_values is not None and all(
            n in frozen_values and isinstance(frozen_values[n], (int, float)) and abs(frozen_values[n]) < 1e300
            for n in fitted["names"])
        if not complete:
            return {"status": "NO PREDICTION", "reason": "frozen inputs for this market are incomplete"}
        p = oc.predict_logistic(fitted, [sh.scoring_row(frozen_values)], calibrated=True)[0]
        row = {"ticker": ticker, "opens_at": str(opens_at), "generated_at": str(generated_at),
               "scored_at": now.isoformat(), "seconds_after_open": round((now - _ts(opens_at)).total_seconds(), 3),
               "inputs_complete": 1, "probability": round(p, 6), "decision": oc.call_of(p),
               "model_sha256": version["sha256"]}
        if sh.write_ledger(ledger, [row]) == 0:             # another writer recorded this market first
            existing = _row_for(ledger, ticker)
            return _from_row(existing) if existing else {"status": "NOT RECORDED", "reason": "ledger write conflict"}
        return _from_row((ticker, row["opens_at"], row["generated_at"], row["scored_at"],
                          row["seconds_after_open"], 1, row["probability"], row["decision"], row["model_sha256"]))
    except Exception as exc:                                # shadow failure is shown, never propagated
        return {"status": "NO PREDICTION", "reason": "shadow error: " + type(exc).__name__}

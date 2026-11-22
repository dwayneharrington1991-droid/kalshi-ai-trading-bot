"""Isolated R10-corrected input pipeline (shadow data collection only).

R10 (regression ledger): today's micro tracker and advanced flow reject most valid Coinbase trades
because quotes and trades share one exchange-time clock and market_trades batches arrive newest-first
(measured 2026-11-22: 935/1,384 valid trades rejected; corrected path keeps 661/661). The live model
was trained on the uncorrected inputs, so the correction cannot simply be switched on.

This module runs a SECOND, corrected copy of both consumers on the same stream observations:
  * MarketIntelligence(per_kind_event_clock=True)
  * AdvancedIntelligence fed with each same-receipt trade batch in exchange-time order
and at each opening writes the corrected micro_* and advanced_* candidate inputs to a separate,
write-once SQLite file. That builds a training set for a model that uses corrected inputs (v5/R10).

Guarantees: it never changes live inputs, predictions, the journal or the dashboard; every error is
contained and counted; recording runs off the opening path in a background thread; only inputs
received at or before the opening are stored.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .advanced import AdvancedIntelligence
from .intelligence import MarketIntelligence

SCHEMA = """
CREATE TABLE IF NOT EXISTS r10_shadow_openings (
    ticker TEXT PRIMARY KEY,
    opens_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    primary_source TEXT,
    values_json TEXT NOT NULL,
    counters_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS r10_no_update BEFORE UPDATE ON r10_shadow_openings
BEGIN SELECT RAISE(ABORT, 'r10 shadow ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS r10_no_delete BEFORE DELETE ON r10_shadow_openings
BEGIN SELECT RAISE(ABORT, 'r10 shadow ledger is append-only'); END;
"""


def _ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def ordered_batches(observations):
    """Same observations, with each run of trades sharing one source and receipt time (one exchange
    message) re-ordered by exchange time and trade id. Receipt order is never changed."""
    out, group = [], []

    def flush():
        group.sort(key=lambda o: (str(o.get("event_at") or ""), _tid(o.get("trade_id"))))
        out.extend(group)
        group.clear()

    for obs in observations:
        is_trade = isinstance(obs, dict) and obs.get("kind") == "trade"
        if group and not (is_trade and obs.get("source") == group[0].get("source")
                          and obs.get("received_at") == group[0].get("received_at")):
            flush()
        if is_trade:
            group.append(obs)
        else:
            out.append(obs)
    flush()
    return out


def _tid(value):
    s = str(value or "")
    return (int(s), s) if s.isdigit() else (-1, s)


class R10Shadow:
    def __init__(self, path):
        self.path = Path(path)
        self.intelligence = MarketIntelligence(per_kind_event_clock=True)
        self.advanced = AdvancedIntelligence()
        self.counters = {"observed": 0, "intelligence_rejected": 0, "advanced_rejected": 0,
                         "recorded": 0, "record_errors": 0}
        self._lock = threading.Lock()
        self._ready = False

    def observe_batch(self, observations):
        for obs in ordered_batches(list(observations or [])):
            self.counters["observed"] += 1
            for name, consumer in (("intelligence_rejected", self.intelligence.observe),
                                   ("advanced_rejected", self.advanced.observe)):
                try:
                    consumer(obs)
                except (KeyError, ValueError, TypeError, AttributeError):
                    self.counters[name] += 1

    def values_at(self, opened, candles=None, primary_source=None, threshold=None):
        """Corrected micro_*/advanced_* candidate inputs received at or before ``opened``."""
        values = {}
        summary = self.intelligence.summarize(opened, candles=candles or {}, primary_source=primary_source)
        primary = summary.get("sources", {}).get(summary.get("primary_source") or primary_source, {})
        for name, value in (summary.get("candidate_features") or {}).items():
            if not name.startswith("micro_") or value is None:
                continue
            window = primary.get("windows", {}).get(name.split("_", 2)[1], {})
            if window.get("status") == "READY" and window.get("last_received_at") \
                    and _ts(window["last_received_at"]) <= opened:
                values[name] = float(value)
        adv = self.advanced.summarize(opened, candles=candles or {}, primary_source=primary_source, threshold=threshold)
        proofs = adv.get("candidate_feature_sources", {})
        for name, value in (adv.get("candidate_features") or {}).items():
            if not name.startswith("advanced_") or value is None:
                continue
            proof = proofs.get(name, {})
            try:
                if _ts(proof["received_at"]) > opened or (proof.get("event_at") and _ts(proof["event_at"]) > opened):
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            values[name] = float(value)
        return values

    def record(self, ticker, opened, candles=None, primary_source=None, threshold=None):
        try:
            values = self.values_at(opened, candles, primary_source, threshold)
            with self._lock:
                with sqlite3.connect(self.path, timeout=5) as db:
                    if not self._ready:
                        db.executescript(SCHEMA)
                        self._ready = True
                    db.execute("INSERT OR IGNORE INTO r10_shadow_openings VALUES (?,?,?,?,?,?)",
                               (ticker, opened.isoformat(), datetime.now(timezone.utc).isoformat(),
                                primary_source, json.dumps(values, sort_keys=True),
                                json.dumps(self.counters, sort_keys=True)))
            self.counters["recorded"] += 1
        except Exception:              # shadow only: never let it reach the live path
            self.counters["record_errors"] += 1

    def record_async(self, *args, **kwargs):
        threading.Thread(target=self.record, args=args, kwargs=kwargs, daemon=True,
                         name="r10-shadow-record").start()

"""Read-only browser dashboard. Start manually; defaults to replay, never trading."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from decimal import Decimal
from pathlib import Path
import re
import math
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs
from time import perf_counter, monotonic
import uuid

from .account_readonly import ReadOnlyKalshi, AccountReadError, credential_status
from .account_state import (
    AccountJournal,
    account_guidance,
    live_trade_cards,
    position_forecast_assessment,
)
from .scanner import context as scanner_context, qualify as scanner_qualify
from .decision import assess as trade_decision
from .decision_coordinator import coordinate as coordinate_intelligence
from .news_intelligence import classify as classify_news
from .contract_rules import snapshot as contract_rule_snapshot
from .settlement_probability import estimate as settlement_probability
from .fair_value import compare as compare_fair_value
from .market_health import selection_summary, diagnose as diagnose_market
from .prediction_engine import (
    view as engine_view,
    opening_forecast,
    performance as engine_performance,
    context24,
)
from .manual import ManualJournal
from .live_model import LiveEstimator
from .learning import load_artifact, predict as learned_predict
from .model import Display, timestamp
from .monitor import Journal, PublicFeed, ReplayFeed, active_market, analyze, utcnow
from .runtime import CachedReport, LatestPayload, RuntimeMeter, background_pool, background_thread_priority
from .intelligence import MarketIntelligence
from .comparison import model_market
from .alerts import MonitoringAlerts, risk_alerts
from .scalp_monitor import diagnose as diagnose_scalp, ScalpCriteria
from .advanced import AdvancedIntelligence, attach_reliability, refresh_status
from .edge_scanner import calculate as calculate_edge_scanner, performance as edge_performance
from .strategy_paper import evaluate as evaluate_dual_strategy
from .multi_asset import MarketStrip

OPENING_INPUT_GRACE_SECONDS = 120
# Pre-opening reference receipts persisted for crash recovery (opening uses 65 s).
PREOPEN_PERSIST_SECONDS = 70
# Same limit context24 applies to an opening's candle receipt.
CANDLE_CACHE_STALE_SECONDS = 150


def candle_integrity_report(source, observed_at):
    """Describe one published candle snapshot against its own immutable cutoff.

    Collector and HTTP workers run independently. Grading the previous
    collector snapshot against the request's newer wall-clock minute creates a
    false 1439/1440 gap at minute rollover. ``window_cutoff`` is the exact
    window the collector asked providers to fill; older payloads derive it
    from their receipt timestamp for compatibility.

    Status precedence (most severe first):
      INVALID    - the snapshot's own timestamps are impossible (a cutoff later
                   than the request clock or than its receipt, a receipt in the
                   future, or a window more than 24 h old). Never deployable.
      STALE      - no receipt timestamp, or the cutoff is older than 120 s, or
                   the receipt is older than the 150 s opening freshness limit.
                   A stale collector is never deployable, complete or not.
      INCOMPLETE - fresh, but a bar is missing, duplicated, unaligned,
                   out of order, or outside the window.
      READY      - fresh, exactly 1,440 unique, ordered, aligned bars.
    """
    source = source if isinstance(source, dict) else {}
    wall_cutoff = int(observed_at.timestamp()) // 60 * 60
    try:
        received = timestamp(source["received_at"])
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        received = None
    try:
        snapshot_cutoff = int(source.get("window_cutoff"))
        cutoff_source = "collector_window_cutoff"
    except (TypeError, ValueError, OverflowError):
        cutoff_source = "received_at_compatibility_fallback"
        snapshot_cutoff = (
            int(received.timestamp()) // 60 * 60 if received is not None else wall_cutoff
        )
    receipt_age = (
        (observed_at - received).total_seconds() if received is not None else None
    )
    invalid_reasons = []
    if snapshot_cutoff % 60:
        invalid_reasons.append("window_cutoff is not minute-aligned")
    if snapshot_cutoff > wall_cutoff:
        invalid_reasons.append("window_cutoff is later than the request clock")
    elif wall_cutoff - snapshot_cutoff > 86400:
        invalid_reasons.append("window_cutoff is more than 24 h old")
    if received is not None:
        # Clock skew allowance between collector and HTTP worker threads is
        # zero in-process; allow 5 s for monotonic/wall adjustments only.
        if receipt_age < -5:
            invalid_reasons.append("received_at is in the future")
        if snapshot_cutoff > int(received.timestamp()) // 60 * 60:
            # A window ending after its own receipt would claim bars that had
            # not closed when the data arrived (future-data leakage).
            invalid_reasons.append("window_cutoff is later than its receipt")
    cutoff_valid = not invalid_reasons
    starts, invalid = [], 0
    for raw in source.get("candles", []):
        try:
            start = int(raw["start"])
            if start % 60:
                raise ValueError("unaligned")
            starts.append(start)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    unique = sorted(set(starts))
    # Measured on the provider order before any sorting, so a reordered
    # publication is visible instead of being silently repaired.
    out_of_order = starts != sorted(starts)
    expected = list(range(snapshot_cutoff - 86400, snapshot_cutoff, 60))
    expected_set = set(expected)
    unique_set = set(unique)
    missing = [stamp for stamp in expected if stamp not in unique_set]
    unexpected = [stamp for stamp in unique if stamp not in expected_set]
    age = wall_cutoff - snapshot_cutoff if cutoff_valid else None
    stale_reasons = []
    if cutoff_valid:
        if received is None:
            stale_reasons.append("no receipt timestamp")
        elif receipt_age > CANDLE_CACHE_STALE_SECONDS:
            stale_reasons.append(
                f"receipt {receipt_age:.0f}s old > {CANDLE_CACHE_STALE_SECONDS}s"
            )
        if age > OPENING_INPUT_GRACE_SECONDS:
            stale_reasons.append(f"cutoff {age}s old > {OPENING_INPUT_GRACE_SECONDS}s")
    structurally_ready = bool(
        cutoff_valid and not missing and not unexpected and len(starts) == 1440
        and len(unique) == 1440 and not invalid and not out_of_order
    )
    status = (
        "INVALID" if not cutoff_valid
        else "STALE" if stale_reasons
        else "READY" if structurally_ready
        else "INCOMPLETE"
    )
    diagnostics = source.get("diagnostics", {})
    return {
        "status": status,
        "invalid_reasons": invalid_reasons,
        "stale_reasons": stale_reasons,
        "cutoff": snapshot_cutoff,
        "wall_clock_cutoff": wall_cutoff,
        "snapshot_age_seconds": age,
        "cache_age_seconds": round(receipt_age, 3) if receipt_age is not None else None,
        "stale_after_seconds": CANDLE_CACHE_STALE_SECONDS,
        "cutoff_source": cutoff_source,
        "interval_seconds": 60,
        "received_at": source.get("received_at"),
        "starts": unique,
        "bars": len(starts),
        "unique_bars": len(unique),
        "duplicate_bars": len(starts) - len(unique),
        "invalid_bars": invalid,
        "out_of_order": out_of_order,
        "missing_intervals": missing,
        "unexpected_intervals": unexpected,
        "recovery": {
            "attempts": diagnostics.get("provider_attempts", []),
            "backoffs": diagnostics.get("provider_backoffs", []),
            "next_retry_at": diagnostics.get("next_retry_at"),
            "reason": diagnostics.get("reason"),
        },
        "diagnostics": diagnostics,
        "note": (
            "Timestamp-only integrity evidence for the collector's published "
            "snapshot; no prices, credentials, account data, or historical rows "
            "are exposed."
        ),
    }


def process_prediction_frame(monitor, feed, collected):
    """Process one latest-value frame and isolate recoverable enrichment faults.

    Returning the exception type to worker telemetry keeps the live thread
    running without exposing provider payloads or exception messages.
    """
    base, started, completed, duration = collected
    try:
        if isinstance(base, Exception):
            monitor.tick(collected)
        else:
            enriched = feed.enriched(base, utcnow())
            monitor.tick((enriched, started, completed, duration))
    except Exception as exc:
        monitor.runtime_meter.error("prediction", type(exc).__name__)
        return exc
    return None


def _eligible_bar_count(item, opened):
    """Closed one-minute bars in the 24 h window that were received by the opening."""
    try:
        received = timestamp(item["received_at"])
        # Same arithmetic as before; the per-bar constants are computed once.
        limit = min(opened.timestamp(), received.timestamp())
        floor = opened.timestamp() - 86520
        return sum(
            1 for bar in item.get("candles", [])
            if int(bar["start"]) + 60 <= limit
            and int(bar["start"]) >= floor
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return -1


class Monitor:
    def __init__(
        self,
        feed,
        journal,
        *,
        target=0.05,
        stop=0.10,
        horizon=60,
        models_enabled=True,
        manual_delay=3,
        slippage=0.01,
        model_artifact=None,
        account_client=None,
        account_store=None,
        model_registry=None,
        learning_status_path=None,
        opening_model_artifact=None,
        build_commit="unknown",
        startup_update=None,
    ):
        startup_update = startup_update or (lambda _stage, _message: None)
        self.feed, self.journal = feed, journal
        # Keep the exact most recently collected candle payload on the monitor.
        # PublicFeed owns asynchronous refreshes, but request handlers must not
        # depend on its internal cache timing after a release restart.
        self.latest_candles = None
        # Receipt-proven histories are retained only across the imminent
        # contract boundary. This prevents an after-open refresh or a
        # transient journal lock from turning a truthful 1439/1440 snapshot
        # into 0/1440. Nothing in this cache may be updated after its opening.
        self.preopening_histories = {}
        # Kraken order-flow observations received at or before each opening.
        self.preopening_flow = {}
        self.candle_lock = threading.Lock()
        from .signal_chart import LiveTradeCandles

        self.live_trade_chart = LiveTradeCandles()
        self.build_commit = build_commit
        self.learning_status_path = (
            Path(learning_status_path) if learning_status_path is not None else None
        )
        self.learning_status_cache = {
            "status": "NOT_CONFIGURED" if learning_status_path is None else "NO_COMPLETED_CYCLE",
            "reason": "No durable scheduled-learning result has been recorded yet",
        }
        self.learning_status_mtime = None
        self.target, self.stop, self.horizon = target, stop, horizon
        self.account_client = account_client
        self.account_candidate = None
        self.account_requested = account_client is not None
        startup_update("ACCOUNT_STORE", "Opening preserved read-only account-monitoring history")
        self.account_journal = AccountJournal(account_store or journal.path)
        if account_client is not None:
            self.account_journal.bind(
                getattr(account_client, "credential_binding", "test-transport")
            )
            Path(self.account_journal.path).chmod(0o600)
        self.account_connected = False
        self.account_error = None
        self.startup_diagnostics = {}
        self.account_job = None
        self.account_started = None
        self.account_forecast_candidates = {}
        self.account_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="account-read-only"
        )
        startup_update("MANUAL_JOURNAL", "Opening preserved manual-trade journal")
        self.manual = ManualJournal(journal.path)
        self.manual_quotes = {}
        self.manual_jobs = {}
        self.manual_pool = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="recorded-contract-public-quotes"
        )
        self.manual_defaults = {
            "target": target,
            "stop": stop,
            "horizon": horizon,
            "slippage": slippage,
        }
        self.signal_key = None
        self.signal_id = None
        self.models_enabled = models_enabled
        startup_update("MODEL_METADATA", "Loading validation-selected model metadata")
        self.artifact, self.artifact_status = load_artifact(model_artifact)
        from .opening_models import load_artifact as load_opening_artifact

        self.opening_artifact, self.opening_artifact_status = load_opening_artifact(
            opening_model_artifact
        )
        startup_update("LEARNING_READINESS", "Reading indexed learning and validation summaries")
        self.scalp_criteria = self.journal.scalp_criteria()
        # Historical readiness can still scan thousands of settled contracts
        # on a multi-gigabyte journal. It is display/research metadata, not a
        # prerequisite for live collection. Start fail-closed and calculate it
        # in the existing background pool after the first fresh market frame.
        self.readiness = {
            "status": "CALCULATING_IN_BACKGROUND",
            "official_contracts": None,
            "minimum_contracts": 170,
            "reason": (
                "Historical learning readiness is loading in the background; "
                "unvalidated models remain ineligible for actionable signals."
            ),
        }
        self.readiness_at = None
        self.estimator = LiveEstimator(manual_delay=manual_delay, slippage=slippage)
        if isinstance(feed, PublicFeed):
            startup_update("LIVE_HISTORY_RESTORE", "Restoring bounded recent timestamped observations")
            self.estimator.restore(journal.recent_states())
        # Request threads can wait for a new immutable state version without
        # polling or blocking the market-data worker.
        self.lock = threading.Condition()
        self.state = dict(
            direction="UNCLEAR",
            scalp="WAIT",
            market=None,
            plans={},
            stale=True,
            now=utcnow().isoformat(),
            remaining_seconds=0,
            estimate=asdict(Display("WAIT", "Waiting for market data")),
            reference={},
            warnings=[],
            mode="STARTING",
        )
        self.settlement_check = None
        self.last_tick = utcnow()
        self.state_version = 0
        self.last_cycle_started = None
        startup_update("ANALYTICS", "Preparing advanced indicators and background evaluators")
        self.intelligence = MarketIntelligence()
        self.advanced = AdvancedIntelligence()
        self.monitoring_alerts = MonitoringAlerts()
        self.market_strip = MarketStrip()
        self.last_proxy_observation = None
        self.runtime_meter = RuntimeMeter()
        # Phone alerts for NEW calibrated UP/DOWN opening calls (signals only;
        # no-op without private credentials; never blocks or fails a tick).
        try:
            from .phone_alerts import from_journal as _phone_alerts
            self.phone_alerts = _phone_alerts(journal.path) if getattr(journal, "path", None) else None
        except Exception:
            self.phone_alerts = None
        self.worker_lock = threading.Lock()
        self.worker_status = {
            "collector": {"status": "NOT_STARTED", "last_started_at": None,
                          "last_completed_at": None, "cycles": 0, "last_error_type": None},
            "prediction": {"status": "NOT_STARTED", "last_started_at": None,
                           "last_completed_at": None, "cycles": 0, "last_error_type": None},
        }
        self.worker_threads = {}
        self.index_maintenance = {"status": "PENDING"}
        self.maintenance_pool = background_pool(1, "settlement-results")
        self.maintenance_job = None
        self.maintenance_error = None
        self.readiness_job = None
        self.readiness_pool = background_pool(1, "learning-readiness")
        self.engine_report = CachedReport(
            self.prediction_performance, interval=30, name="prediction-performance"
        )
        self.research_report = CachedReport(
            self.journal.performance, interval=60, name="historical-research"
        )
        from .scalp_paper import evaluate_journal

        self.scalp_paper_report = CachedReport(
            lambda: evaluate_journal(self.journal.path), interval=60, name="book-replay-evaluation"
        )
        self.edge_report = CachedReport(
            self.edge_performance, interval=30, name="edge-performance"
        )
        self.quick_learning_report = CachedReport(
            self.journal.quick_learning_summary, interval=30, name="quick-learning-summary"
        )
        self.opening_scoreboard_report = CachedReport(
            self.journal.verified_opening_scoreboard, interval=30, name="opening-scoreboard"
        )
        self.scheduled_learning = None
        self.scheduled_scalp_learning = None
        if model_registry is not None:
            from .model_registry import scheduled_evaluation
            from .scalp_learning import scheduled_evaluation as scheduled_scalp_evaluation

            self.scheduled_learning = CachedReport(
                lambda: scheduled_evaluation(self.journal.path, model_registry),
                interval=3600,
                name="scheduled-offline-model-comparison",
            )
            self.scheduled_scalp_learning = CachedReport(
                lambda: scheduled_scalp_evaluation(
                    self.journal.path, Path(model_registry) / "latest-scalp-evaluation.json"
                ),
                interval=3600,
                name="scheduled-scalp-shadow-evaluation",
            )
        startup_update("MONITOR_READY", "Intelligence monitor initialized")

    def background_learning_status(self):
        """Read the small atomic scheduler result without touching model state."""
        path = self.learning_status_path
        if path is None:
            return dict(self.learning_status_cache)
        try:
            stat = path.stat()
            if stat.st_size > 65536:
                raise ValueError("status file exceeds bounded size")
            if self.learning_status_mtime != stat.st_mtime_ns:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("automated_trading") != "DISABLED":
                    raise ValueError("invalid learning status safety marker")
                self.learning_status_cache = value
                self.learning_status_mtime = stat.st_mtime_ns
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.learning_status_cache = {
                "status": "STATUS_UNREADABLE",
                "error_type": type(exc).__name__,
                "reason": "Scheduled-learning status is unavailable; live inference continues independently",
                "automated_trading": "DISABLED",
            }
        result = dict(self.learning_status_cache)
        try:
            finished = timestamp(result["finished_at"])
            result["age_seconds"] = max(0, (utcnow() - finished).total_seconds())
            result["stale"] = result["age_seconds"] > 26 * 3600
        except (KeyError, ValueError, TypeError, AttributeError):
            result["age_seconds"] = None
            result["stale"] = True
        return result

    def worker_event(self, name, event, error=None):
        with self.worker_lock:
            item = self.worker_status[name]
            now = utcnow().isoformat()
            if event == "started":
                item.update(status="RUNNING", last_started_at=now)
            elif event == "completed":
                item.update(
                    status="RUNNING", last_completed_at=now,
                    cycles=item["cycles"] + 1,
                    last_error_type=type(error).__name__ if error else None,
                )
            elif event == "stopped":
                item.update(status="STOPPED", last_error_type=type(error).__name__ if error else None)

    def workers_health(self, now=None):
        now = now or utcnow()
        with self.worker_lock:
            values = {name: dict(item) for name, item in self.worker_status.items()}
        healthy = True
        reasons = []
        required = (
            {"collector", "prediction"} if isinstance(self.feed, PublicFeed)
            else {"prediction"} if isinstance(self.feed, ReplayFeed)
            else set()
        )
        for name, item in values.items():
            thread = self.worker_threads.get(name)
            item["thread_alive"] = bool(thread and thread.is_alive())
            completed = item.get("last_completed_at")
            item["seconds_since_completion"] = (
                max(0, (now - timestamp(completed)).total_seconds()) if completed else None
            )
            # A public collection pass is bounded by endpoint timeouts. Thirty
            # seconds allows a slow provider pass without hiding a dead thread.
            if name not in required:
                continue
            if not item["thread_alive"]:
                healthy = False
                reasons.append(name + " worker is not alive")
            elif completed and item["seconds_since_completion"] > 30:
                healthy = False
                reasons.append(name + " worker has not completed a cycle in 30 seconds")
        return {"healthy": healthy, "reasons": reasons, "workers": values}

    def prediction_performance(self):
        with self.journal.read() as db:
            return engine_performance(db, utcnow())

    def edge_performance(self):
        with self.journal.read() as db:
            return edge_performance(db)

    def maintain_settlements(self, now):
        # Older settled forecasts may predate the compact review table. Process
        # a bounded batch in maintenance so live collection is never blocked.
        self.journal.backfill_contract_summaries(limit=20)
        self.journal.backfill_contract_reviews(limit=20)
        self.journal.seed_settlement_queue()
        learned = False
        for ticker in self.journal.due_settlements(now):
            self.journal.mark_settlement_attempt(ticker, now)
            try:
                outcome = self.feed.resolved(ticker)
                learned = self.journal.settle(
                    ticker, outcome, "PUBLIC MARKET DATA"
                ) or learned
                if outcome is not None:
                    self.maintenance_error = None
            except Exception:
                # No stale or fabricated outcome on HTTP failure.
                self.maintenance_error = "Official settlement result pending or inaccessible"
        if learned:
            # Score the new independent outcome immediately. Expensive candidate
            # fitting remains background/daily and its own due gate still applies.
            self.engine_report.invalidate()
            self.research_report.invalidate()
            self.edge_report.invalidate()
            scoreboard = getattr(self, "opening_scoreboard_report", None)
            if scoreboard is not None:
                scoreboard.invalidate()
            # Force the displayed official-label count to refresh immediately
            # after a new result instead of retaining the prior 60-second cache.
            self.readiness_at = None
            if self.scheduled_learning:
                self.scheduled_learning.invalidate()
            if self.scheduled_scalp_learning:
                self.scheduled_scalp_learning.invalidate()

    def update_maintenance(self, now):
        if self.maintenance_job is not None and self.maintenance_job.done():
            try:
                self.maintenance_job.result()
            except Exception:
                self.maintenance_error = (
                    "Settlement maintenance unavailable; retrying in background"
                )
            self.maintenance_job = None
        if self.maintenance_job is None and (
            self.settlement_check is None or (now - self.settlement_check).total_seconds() >= 60
        ):
            self.settlement_check = now
            self.maintenance_job = self.maintenance_pool.submit(self.maintain_settlements, now)

    def accept_candle_batch(self, payload, received_at=None):
        """Publish and persist verified closed candles at the collector boundary.

        Public candle preload and prediction run on separate workers. Keeping
        this handoff before the prediction mailbox prevents a healthy 1,440-bar
        preload from temporarily producing an empty chart. The journal keeps
        the provider's original timestamps; signal markers remain independent
        append-only receipts and are never synthesized here.
        """
        candles = payload.get("btc_candles") if isinstance(payload, dict) else None
        if not isinstance(candles, dict) or not candles.get("candles"):
            return False
        observed = received_at or utcnow()
        persisted = True
        with self.candle_lock:
            try:
                self.journal.record_candles(candles, observed)
            except Exception as exc:
                # Disk pressure or a transient SQLite writer conflict must not
                # kill the collector and freeze every live panel.  The exact
                # provider candles remain available to the chart fallback,
                # while health diagnostics explicitly report persistence loss.
                self.runtime_meter.error("candle_persistence", type(exc).__name__)
                persisted = False
            try:
                # The provider batch receipt defines its rolling window. This
                # also keeps deterministic replay histories anchored to their
                # original clock instead of the machine's current wall clock.
                recovery_cutoff = candles.get("received_at") or observed
                candles = self.journal.recover_recorded_candles(candles, recovery_cutoff)
                payload["btc_candles"] = candles
            except Exception as exc:
                # A read lock/corrupt row never licenses a guessed candle. Keep
                # the provider batch and expose the recovery failure.
                diagnostics = dict(candles.get("diagnostics") or {})
                diagnostics["journal_recovery"] = {
                    "status": "UNAVAILABLE", "error_type": type(exc).__name__
                }
                candles = {**candles, "diagnostics": diagnostics}
                payload["btc_candles"] = candles
                self.runtime_meter.error("candle_recovery", type(exc).__name__)
            self.latest_candles = candles
        return persisted

    def opening_reference_points(self, opened, mode):
        """Pre-opening reference points: in-memory receipts first; only when
        none exist (e.g. after a restart) the journal's receipt-proven
        pre-opening ticks. Returns (points, source, recovery_diagnostics)."""
        points, source = self.estimator.opening_points(opened)
        if points:
            return points, source, None
        try:
            recovered, recovered_source, diagnostics = self.journal.preopen_reference_points(opened, mode)
        except sqlite3.OperationalError as exc:
            self.runtime_meter.error("preopen_reference_recovery", type(exc).__name__)
            return [], None, {"status": "STORAGE_UNAVAILABLE"}
        if not recovered:
            return [], None, diagnostics
        return recovered, f"{recovered_source} [journal-recovered pre-opening receipts]", diagnostics

    def archived_opening_inputs(self, opened):
        """Journal archive of receipt-proven pre-opening candles, memoised for
        5 s: the grace-window retries otherwise re-read it on every tick."""
        cache = getattr(self, "_archive_cache", {})
        wall = utcnow()
        hit = cache.get(opened)
        if hit and 0 <= (wall - hit[0]).total_seconds() < 5:
            return hit[1]
        value = self.journal.opening_inputs(opened)
        self._archive_cache = {k: v for k, v in cache.items() if (wall - v[0]).total_seconds() < 60}
        self._archive_cache[opened] = (wall, value)
        return value

    @staticmethod
    def opening_inputs_complete(history, points, opened):
        """True only when these inputs pass the opening model's own input
        checks: a READY cutoff-safe 24 h window received by the opening and a
        reference receipt no more than 5 s before it. Mirrors opening_forecast."""
        try:
            if timestamp(history["received_at"]) > opened:
                return False
            if context24(history, opened).get("status") != "READY":
                return False
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        fresh = [when for when, _ in points if opened - timedelta(seconds=65) <= when < opened]
        return bool(fresh) and (opened - max(fresh)).total_seconds() <= 5

    def remember_preopening_histories(self, payload, now):
        """Retain bounded, pre-open-only candle receipts for rollover."""
        raw = payload.get("btc_candles", {}) if isinstance(payload, dict) else {}
        try:
            received = timestamp(raw["received_at"])
        except (KeyError, ValueError, TypeError, AttributeError):
            received = None
        retained = {}
        for market in payload.get("markets", []) if isinstance(payload, dict) else []:
            try:
                ticker = market["ticker"]
                opened = timestamp(market["opens_at"])
                closes = timestamp(market["closes_at"])
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            prior = self.preopening_histories.get(ticker)
            if prior is not None and now < closes:
                retained[ticker] = prior
            if (
                received is not None
                and received <= opened
                and now <= opened
                and raw.get("candles")
            ):
                # PublicFeed replaces rather than mutates history objects, but
                # a deep copy makes the opening boundary explicit and testable.
                try:
                    unchanged = (
                        prior is not None
                        and timestamp(prior["received_at"]) == received
                    )
                except (KeyError, ValueError, TypeError, AttributeError):
                    unchanged = False
                if not unchanged:
                    retained[ticker] = deepcopy(raw)
        self.preopening_histories = dict(list(retained.items())[-12:])

    def remember_preopening_flow(self, payload, now):
        """Keep the newest pre-open Kraken order-flow item per upcoming contract.

        Only observations received at or before the opening are retained, so a
        delayed opening forecast can still use genuinely as-of-opening flow.
        Bounded and in memory: after a restart these features stay unavailable.
        """
        coverage = (payload or {}).get("coverage") or {}
        entry = coverage.get("Kraken public order flow") if isinstance(coverage, dict) else None
        flow = (entry.get("data") if isinstance(entry, dict) else None) or {}
        if not isinstance(flow, dict):
            flow = {}
        retained = {}
        for market in (payload or {}).get("markets", []) or []:
            if not isinstance(market, dict):
                continue
            try:
                ticker = market["ticker"]
                opened = timestamp(market["opens_at"])
                closes = timestamp(market["closes_at"])
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            prior = dict(self.preopening_flow.get(ticker) or {})
            if now >= closes:
                continue
            if now <= opened + timedelta(seconds=5):
                for key in ("spot", "perpetual", "spot_book"):
                    item = flow.get(key)
                    if not isinstance(item, dict):
                        continue
                    try:
                        observed = timestamp(item["observed_at"])
                    except (KeyError, ValueError, TypeError, AttributeError):
                        continue
                    if item.get("status") != "LIVE" or observed > opened or observed > now:
                        continue
                    previous = prior.get(key)
                    if previous is None or timestamp(previous["observed_at"]) < observed:
                        prior[key] = deepcopy(item)
            if prior:
                retained[ticker] = prior
        self.preopening_flow = dict(list(retained.items())[-12:])

    def tick(self, collected=None):
        tick_started = perf_counter()
        phases = {}
        try:
            if collected is None:
                collection_started = utcnow()
                payload = self.feed.poll()
                collection_completed = utcnow()
                collection_ms = (perf_counter() - tick_started) * 1000
            else:
                payload, collection_started, collection_completed, collection_ms = collected
                if isinstance(payload, Exception):
                    raise payload
            phases["market_collection"] = round(collection_ms, 2)
            # Replay mode and prediction-worker recovery still use this path;
            # the public collector normally accepted the same immutable batch
            # before placing it in the latest-value mailbox.
            self.accept_candle_batch(payload, utcnow())
            calculation_started = perf_counter()
            now = timestamp(payload["clock"]) if "clock" in payload else utcnow()
            coverage = payload.setdefault("coverage", {})
            ref = payload.get("reference", {})
            coverage["Coinbase BTC/USD proxy"] = {
                "status": "CONNECTED" if ref.get("price") is not None else "UNAVAILABLE",
                "data": ref,
                "reason": None if ref.get("price") is not None else "Public BTC proxy unavailable",
            }
            market_now = active_market(payload, now)
            rejected = 0
            for observation in payload.get("stream_observations", []):
                self.live_trade_chart.observe(observation)
                # Independent consumers must not discard valid trade-channel
                # volume merely because the legacy price sampler rejected an
                # older cross-channel event timestamp.
                for consumer in (self.intelligence.observe, self.advanced.observe):
                    try:
                        consumer(observation)
                    except (KeyError, ValueError, TypeError, AttributeError):
                        rejected += 1
                self.estimator.observe_reference(observation, now)
            # Freeze opening inputs before optional analytics. This artifact is
            # independent of live-data freshness and remains usable when
            # threshold metadata or model generation arrives later.
            mode = payload.get("mode", "PUBLIC MARKET DATA")
            # Crash-safe pre-opening reference receipts (Phase 5). Written with
            # the wall-clock persistence time so recovery can prove each one
            # was stored promptly after receipt, i.e. before the opening.
            try:
                # Peek/ack rather than drain-before-write. If SQLite is busy or
                # the transaction fails, the receipts remain queued for the
                # next tick instead of being silently lost.
                pending = self.estimator.peek_preopen_references()
                opens = set()
                for listed in payload.get("markets", []) or ([market_now] if market_now else []):
                    try:
                        opens.add(timestamp(listed["opens_at"]))
                    except (KeyError, TypeError, ValueError, AttributeError):
                        continue
                ticks = []
                for source, received, price in pending:
                    # The next contract may not be listed until it opens, so
                    # the next quarter-hour boundary (KXBTC15M schedule) is
                    # also kept. Recovery still requires an exact match with
                    # the actual opening time, so an extra key is harmless.
                    boundary = datetime.fromtimestamp(
                        math.ceil(received.timestamp() / 900) * 900, timezone.utc
                    )
                    for opened in opens | {boundary}:
                        if 0 <= (opened - received).total_seconds() <= PREOPEN_PERSIST_SECONDS:
                            ticks.append((source, received, price, opened))
                if ticks:
                    self.journal.record_preopen_references(
                        ticks, mode, utcnow() if mode == "PUBLIC MARKET DATA" else now
                    )
                self.estimator.acknowledge_preopen_references(len(pending))
            except Exception as exc:
                self.runtime_meter.error("preopen_reference_persistence", type(exc).__name__)
            self.remember_preopening_histories(payload, now)
            # Optional evidence capture. A failure here is recorded but must never
            # turn the tick into a market-data failure (stale state -> /readyz 503).
            try:
                self.remember_preopening_flow(payload, now)
            except Exception as exc:
                self.runtime_meter.error("preopening_flow_capture", type(exc).__name__)
            # Every listed market: the next contract's pre-open book and the new
            # contract's first two minutes (the recorder enforces the window).
            for listed in payload.get("markets", []) or ([market_now] if market_now else []):
                try:
                    self.journal.record_opening_quote(listed, mode, now)
                except Exception as exc:
                    self.runtime_meter.error("opening_quote_capture", type(exc).__name__)
                    break
            for discovered in payload.get("markets", []):
                try:
                    opened = timestamp(discovered["opens_at"])
                    if not (
                        opened <= now
                        <= opened + timedelta(seconds=OPENING_INPUT_GRACE_SECONDS)
                    ):
                        continue
                    raw_history = payload.get("btc_candles", {})
                    try:
                        eligible = timestamp(raw_history["received_at"]) <= opened
                    except (KeyError, ValueError, TypeError, AttributeError):
                        eligible = False
                    existing = self.journal.opening_snapshot(discovered["ticker"], mode)
                    candidates = [
                        item for item in (
                            raw_history if eligible else None,
                            self.preopening_histories.get(discovered["ticker"]),
                            (existing or {}).get("history"),
                        )
                        if isinstance(item, dict)
                    ]

                    def eligible_count(item):
                        try:
                            received = timestamp(item["received_at"])
                            # Same arithmetic; per-bar constants computed once.
                            limit = min(opened.timestamp(), received.timestamp())
                            floor = opened.timestamp() - 86520
                            return sum(
                                1 for bar in item.get("candles", [])
                                if int(bar["start"]) + 60 <= limit
                                and int(bar["start"]) >= floor
                            )
                        except (KeyError, ValueError, TypeError, AttributeError):
                            return -1

                    # The archive can be very large. Consult it only when the
                    # in-memory collector and the already-frozen snapshot do
                    # not contain a complete cutoff-safe window. This keeps
                    # normal live updates independent of journal size.
                    best_count = max((eligible_count(item) for item in candidates), default=-1)
                    if best_count < 1440 and not existing:
                        archived = self.archived_opening_inputs(opened)
                        if isinstance(archived, dict):
                            candidates.append(archived)
                    frozen_history = max(candidates, key=eligible_count) if candidates else {}
                    frozen_points, frozen_source, _recovery = self.opening_reference_points(opened, mode)
                    # Phase 5 repair: an immutable snapshot used to be frozen on
                    # the first tick even when incomplete, so every retry inside
                    # the grace window reused the same gap. Freeze only inputs
                    # that pass the opening model's own checks; otherwise keep
                    # retrying from receipt-proven pre-opening sources.
                    if not existing and not self.opening_inputs_complete(frozen_history, frozen_points, opened):
                        continue
                    self.journal.freeze_opening_snapshot(
                        discovered,
                        mode,
                        frozen_history,
                        frozen_points,
                        frozen_source,
                        now,
                        finalized=True,
                    )
                except sqlite3.OperationalError as exc:
                    self.runtime_meter.error(
                        "opening_snapshot_storage", type(exc).__name__
                    )
                    continue
                except (KeyError, ValueError, TypeError, AttributeError):
                    continue
            primary = None
            if ref.get("transport") == "public WebSocket":
                primary = (
                    "Coinbase BTC/USD stream"
                    if "Coinbase" in ref.get("source", "")
                    else "Kraken BTC/USD stream"
                )
            elif ref.get("price") is not None and ref.get("observed_at"):
                primary = ref.get("source", "BTC REST proxy")
                key = (primary, ref["observed_at"])
                if key != self.last_proxy_observation:
                    try:
                        self.intelligence.ingest(
                            primary,
                            ref["price"],
                            event_at=ref.get("event_time"),
                            received_at=ref["observed_at"],
                            kind="quote",
                        )
                        self.last_proxy_observation = key
                    except (KeyError, ValueError, TypeError, AttributeError):
                        rejected += 1
            intelligence = self.intelligence.summarize(
                now, candles=payload.get("btc_candles", {}), primary_source=primary
            )
            intelligence["rejected_observations_this_update"] = rejected
            payload["market_intelligence"] = intelligence
            advanced_started = perf_counter()
            advanced = self.advanced.summarize(
                now,
                candles=payload.get("btc_candles", {}),
                primary_source=primary,
                threshold=market_now.get("strike") if market_now else None,
            )
            payload["advanced_intelligence"] = advanced
            phases["advanced_analysis"] = round((perf_counter() - advanced_started) * 1000, 2)
            coverage["Kalshi KXBTC15M public"] = {
                "status": "CONNECTED" if market_now else "UNAVAILABLE",
                "reason": "Public contract metadata and executable quotes only; no account access"
                if market_now
                else "No unique current contract",
            }
            cross = coverage.get("Kraken spot", {})
            if cross.get("status") == "CONNECTED" and ref.get("price"):
                if abs(cross["data"]["price"] / ref["price"] - 1) > 0.02:
                    cross.update(
                        status="EXCLUDED",
                        reason="Cross-venue price outlier above 2%; excluded from features",
                    )
            result, info = None, None
            if isinstance(self.feed, PublicFeed):
                if self.models_enabled:
                    result, info = self.estimator.estimate(
                        payload,
                        active_market(payload, now),
                        now,
                        self.target,
                        self.stop,
                        self.horizon,
                    )
                else:
                    result = Display("WAIT", "MODEL_DISABLED: started with --disable-model")
                    info = {
                        "enabled": False,
                        "overall_reason": result.reason,
                        "scalp_reason": result.reason,
                    }
            if info is not None and isinstance(self.feed, PublicFeed):
                if self.readiness_job is not None and self.readiness_job.done():
                    try:
                        self.readiness = self.readiness_job.result()
                    except Exception:
                        self.readiness = {"reason": "Historical readiness calculation unavailable"}
                    self.readiness_job = None
                if self.readiness_job is None and (
                    self.readiness_at is None or (now - self.readiness_at).total_seconds() >= 60
                ):
                    self.readiness_job = self.readiness_pool.submit(self.journal.learning_readiness)
                    self.readiness_at = now
                info["learning_status"] = {
                    **self.readiness,
                    "artifact_status": self.artifact_status,
                    "trained_candidate_loaded": self.artifact is not None,
                    "active": "experimental statistical fallback",
                }
                info["market_intelligence"] = intelligence
                if self.scheduled_learning is not None:
                    evaluation = self.scheduled_learning.get()
                    info["learning_status"]["scheduled_evaluation"] = {
                        "status": evaluation["status"],
                        "last_completed_at": evaluation["generated_at"],
                        "result": evaluation["value"],
                        "active_model_changed": False,
                    }
                if self.scheduled_scalp_learning is not None:
                    scalp_evaluation = self.scheduled_scalp_learning.get()
                    info["learning_status"]["scheduled_scalp_evaluation"] = {
                        "status": scalp_evaluation["status"],
                        "last_completed_at": scalp_evaluation["generated_at"],
                        "result": scalp_evaluation["value"],
                        "active_model_changed": False,
                        "actionable": False,
                    }
                if self.artifact is not None and result.settlement_up is not None:
                    if timestamp(self.artifact["labels_available_through"]) < now:
                        p = learned_predict(
                            self.artifact["model"],
                            info["candidate_features"]["values"],
                            baseline_probability=info.get("baseline_probability_up"),
                        )
                        from dataclasses import replace

                        result = replace(result, settlement_up=p, settlement_down=1 - p)
                        if info.get("price_forecast"):
                            info["price_forecast"] = {
                                **info["price_forecast"],
                                "forecast_model": "Independent zero-drift statistical baseline, separate from selected binary classifier",
                                "interval_label": info["price_forecast"]["interval_label"]
                                + "; price distribution belongs to the independent zero-drift baseline, not the selected binary classifier",
                            }
                        sensitivity = [p]
                        inputs = info.get("settlement_inputs", {})
                        basis = info.get("basis", {})
                        baseline_sensitivity = info.get("settlement_probability_sensitivity") or [
                            None,
                            None,
                        ]
                        for index, bound in enumerate(("low", "high")):
                            if inputs.get("proxy_price") and basis.get(bound) is not None:
                                f = dict(info["candidate_features"]["values"])
                                f["distance_scaled"] = (
                                    inputs["proxy_price"] + basis[bound] - inputs["strike"]
                                ) / inputs["proxy_price"]
                                sensitivity.append(
                                    learned_predict(
                                        self.artifact["model"],
                                        f,
                                        baseline_probability=baseline_sensitivity[index],
                                    )
                                )
                        info["settlement_probability_sensitivity"] = [
                            min(sensitivity),
                            max(sensitivity),
                        ]
                        info["learning_status"].update(
                            active=self.artifact["model"]["kind"],
                            heldout_report=self.artifact["report"],
                            model_id=self.artifact["model_id"],
                            labels_available_through=self.artifact["labels_available_through"],
                        )
                        info["version"] += "/" + self.artifact["model_id"]
                        info[
                            "overall_reason"
                        ] = "EXPERIMENTAL_LEARNED_FORECAST: validation-selected independent classifier; see held-out scores; edge unverified"
                    else:
                        info["learning_status"][
                            "artifact_status"
                        ] = "Future-trained artifact rejected at this prediction time"
            state = analyze(payload, now, self.target, self.stop, self.horizon, result, info)
            rules = contract_rule_snapshot(
                market_now or {}, getattr(self.feed, "last_series", {}) or {},
                received_at=(market_now or {}).get("metadata_received_at", now.isoformat()),
            )
            state["contract_rules"] = (
                (market_now or {}).get("contract_rule_snapshot")
                or (rules.as_dict() if hasattr(rules, "as_dict") else rules)
            )
            probability = {"status": "UNAVAILABLE", "reason": "Contract or volatility inputs unavailable", "actionable": False}
            fair_values = {}
            if hasattr(rules, "as_dict"):
                context = state.get("model", {}).get("btc_context", {})
                probability = settlement_probability(
                    reference=state.get("reference", {}).get("price"),
                    strike=rules.strike,
                    seconds_remaining=state.get("remaining_seconds"),
                    realized_volatility_per_sqrt_second=context.get(
                        "volatility_dollars_per_sqrt_second"
                    ),
                    jump_volatility_per_sqrt_second=0,
                    data_uncertainty=0,
                    comparison_operator=rules.comparison_operator,
                    averaging_remaining_fraction=1,
                )
                yes_probability = probability.get("probability_yes")
                for side, side_probability in (
                    ("up", yes_probability),
                    ("down", str(Decimal(1) - Decimal(yes_probability)) if yes_probability is not None else None),
                ):
                    quote = (state.get("market") or {}).get("quotes", {}).get(side, {})
                    fair_values[side] = compare_fair_value(
                        probability=side_probability,
                        bid=quote.get("bid"), ask=quote.get("ask"),
                        ask_depth=quote.get("ask_size"), quantity=1,
                        fee_multiplier=(state.get("market") or {}).get("fee_multiplier"),
                        price_increment=rules.price_increment, slippage_ticks=1,
                    )
            state["settlement_intelligence"] = {
                "rule_snapshot": state["contract_rules"],
                "time_to_settlement": probability,
                "fair_values": fair_values,
                "official_reference": {
                    "status": "UNAVAILABLE_NO_AUTHORIZED_REALTIME_FEED",
                    "source": getattr(rules, "settlement_source", None),
                },
                "estimated_reference": {
                    "status": "AVAILABLE" if state.get("reference", {}).get("price") is not None else "UNAVAILABLE",
                    "source": state.get("reference", {}).get("source"),
                    "official": False,
                    "warning": "External proxy; not the official settlement index.",
                },
                "model_status": "SHADOW_UNCALIBRATED",
                "actionable": False,
            }
            strip_markets = list(payload.get("multi_asset_markets", []))
            strip_references = dict(payload.get("multi_asset_references", {}))
            strip_histories = dict(payload.get("multi_asset_histories", {}))
            state["market_strip"] = self.market_strip.update(
                strip_markets, strip_references, strip_histories, now
            )
            state["advanced_intelligence"] = self.advanced.decorate(
                advanced,
                info or {},
                now,
                artifact=self.artifact
                if info and info.get("learning_status", {}).get("model_id")
                else None,
                probability=state.get("estimate", {}).get("settlement_up"),
            )
            if info is not None:
                scan = scanner_context(payload, market_now, info, now)
                info["scanner"] = scan
                scanner_qualify(state, scan)
            state["discovered_contracts"] = selection_summary(payload.get("markets", []), now)
            state["streaming"] = payload.get("streaming", {})
            state["historical_preload"] = (payload.get("btc_candles") or {}).get(
                "diagnostics", {"status": "UNAVAILABLE"}
            )
            state["multi_asset_discovery"] = payload.get(
                "multi_asset_diagnostics", {"status": "UNAVAILABLE"}
            )
            state["market_health"] = diagnose_market(state, now)
            try:
                prior = (
                    self.journal.engine_prior(state["market"]["ticker"], state["mode"])
                    if state.get("market")
                    else None
                )
            except sqlite3.OperationalError as exc:
                self.runtime_meter.error("opening_storage", type(exc).__name__)
                prior = None
            opening = None
            try:
                if state.get("market") and self.models_enabled:
                    m = state["market"]
                    try:
                        opening = self.journal.opening_record(m["ticker"], state["mode"])
                    except sqlite3.OperationalError as exc:
                        self.runtime_meter.error("opening_storage", type(exc).__name__)
                        opening = {
                            "status": "UNAVAILABLE",
                            "direction": "WAIT",
                            "up": None,
                            "down": None,
                            "reason": (
                                "OPENING_STORAGE_UNAVAILABLE: immutable journal is busy; "
                                "no opening call will be inferred or rewritten"
                            ),
                        }
                    if (
                        opening is None
                        and m.get("strike") is not None
                        and m.get("mapping_verified")
                        and m.get("averaging_rule_verified")
                        and m.get("comparison_operator") == ">="
                    ):
                        opened = timestamp(m["opens_at"])
                        snapshot = self.journal.opening_snapshot(m["ticker"], state["mode"])
                        if snapshot:
                            frozen = snapshot.get("history", {})
                            frozen_points = [
                                (timestamp(point[0]), point[1])
                                for point in snapshot.get("reference_points", [])
                            ]
                            frozen_source = snapshot.get("reference_source")
                        else:
                            raw_history = payload.get("btc_candles", {})
                            try:
                                eligible = timestamp(raw_history["received_at"]) <= opened
                            except (KeyError, ValueError, TypeError, AttributeError):
                                eligible = False
                            frozen = raw_history if eligible else self.journal.opening_inputs(opened)
                            frozen_points, frozen_source = self.estimator.opening_points(opened)
                        generated = utcnow() if state["mode"] == "PUBLIC MARKET DATA" else now

                        def forecast(history, points, source):
                            return opening_forecast(
                                m,
                                history,
                                points,
                                generated,
                                reference_source=source,
                                audit_context={
                                    "official_contracts": self.readiness.get("official_contracts"),
                                    "minimum_contracts": self.readiness.get("minimum_contracts", 170),
                                    "artifact_status": self.opening_artifact_status,
                                    "live_model": self.artifact.get("model", {}).get("kind")
                                    if self.artifact
                                    else "experimental statistical fallback",
                                    "live_ensemble_active": bool(
                                        self.artifact
                                        and self.artifact.get("model", {}).get("kind") == "ensemble"
                                    ),
                                    "opening_candidate_status": self.opening_artifact_status,
                                    # The fallback quality guard judges the fallback on
                                    # its own probability-implied record only.
                                    "opening_forward_accuracy": self.readiness.get(
                                        "opening_fallback_forward_accuracy",
                                        self.readiness.get("opening_forward_accuracy", {}),
                                    ),
                                },
                                frozen_context={
                                    **self.intelligence.summarize(
                                        opened, candles=history, primary_source=primary
                                    ),
                                    "primary_source": primary,
                                    "order_flow": self.preopening_flow.get(m["ticker"]),
                                    "advanced_features": self.advanced.summarize(
                                        opened,
                                        candles=history,
                                        primary_source=primary,
                                        threshold=m["strike"],
                                    ),
                                },
                                candidate_artifact=self.opening_artifact,
                            )

                        # The original input path always runs first, so an
                        # opening that was already valid is computed exactly as
                        # before. Only when it yields no probability is a second
                        # attempt made from other RECEIPT-PROVEN pre-opening
                        # inputs (Phase 5 repair). Nothing received after the
                        # opening can enter: histories are cutoff-checked by
                        # context24 and reference receipts must precede the open.
                        candidate = forecast(frozen, frozen_points, frozen_source)
                        if snapshot and "journal-recovered" in str(frozen_source) and candidate.get("up") is not None:
                            candidate["input_recovery"] = {
                                "status": "RECOVERED_FROM_RECEIPT_PROVEN_PREOPENING_INPUTS",
                                "history_replaced": False,
                                "reference_points_recovered_from_journal": True,
                                "via": "complete opening snapshot frozen from journal-recovered receipts",
                            }
                        if candidate.get("up") is None and not snapshot:
                            options = [frozen, self.preopening_histories.get(m["ticker"])]
                            if frozen is raw_history:
                                options.append(self.archived_opening_inputs(opened))
                            options = [o for o in options if isinstance(o, dict) and o]
                            alt_history = max(options, key=lambda o: _eligible_bar_count(o, opened)) if options else frozen
                            alt_points, alt_source, recovery = self.opening_reference_points(opened, state["mode"])
                            if alt_history is not frozen or alt_points != frozen_points:
                                retried = forecast(alt_history, alt_points, alt_source)
                                if retried.get("up") is not None:
                                    retried["input_recovery"] = {
                                        "status": "RECOVERED_FROM_RECEIPT_PROVEN_PREOPENING_INPUTS",
                                        "history_replaced": alt_history is not frozen,
                                        "reference_points_recovered_from_journal": bool(recovery),
                                        "reference_recovery": recovery,
                                        "primary_failure_reason": candidate.get("reason"),
                                    }
                                    candidate = retried
                        if state["mode"] == "PUBLIC MARKET DATA":
                            generated = utcnow()
                            candidate["timestamp"] = candidate["generated_at"] = generated.isoformat()
                            candidate["opening_delay_seconds"] = max(
                                0, (generated - opened).total_seconds()
                            )
                            if candidate.get("up") is not None:
                                candidate["status"] = "DELAYED" if generated > opened else "ON_TIME"
                        candidate["closes_at"] = m["closes_at"]
                        # Keep retrying briefly when asynchronous history/reference
                        # delivery straddles rollover. Inputs remain cut off at the
                        # opening instant; later observations can never be backfilled.
                        retry_until = opened + timedelta(seconds=OPENING_INPUT_GRACE_SECONDS)
                        if candidate.get("up") is not None or generated >= retry_until:
                            try:
                                opening = self.journal.opening_record(
                                    m["ticker"], state["mode"], candidate, generated
                                )
                            except sqlite3.OperationalError as exc:
                                self.runtime_meter.error("opening_storage", type(exc).__name__)
                                opening = {
                                    "status": "UNAVAILABLE",
                                    "direction": "WAIT",
                                    "up": None,
                                    "down": None,
                                    "reason": (
                                        "OPENING_STORAGE_UNAVAILABLE: immutable journal is busy; "
                                        "no opening call will be inferred or rewritten"
                                    ),
                                }
                        else:
                            candidate["status"] = "WARMING UP"
                            candidate["reason"] += (
                                "; retrying receipt-proven pre-opening inputs while the bounded "
                                f"{OPENING_INPUT_GRACE_SECONDS}-second rollover window remains open"
                            )
                            candidate["retry_until"] = retry_until.isoformat()
                            opening = candidate
            except Exception as exc:
                # The immutable opening is optional for market readiness. An
                # unexpected error is recorded and the opening is shown as
                # unavailable (WAIT, no confidence) instead of failing every tick
                # for the whole contract, which made the state stale (/readyz 503).
                self.runtime_meter.error("opening_generation", type(exc).__name__)
                opening = {
                    "status": "UNAVAILABLE",
                    "up": None,
                    "direction": "WAIT",
                    "reason": "OPENING_GENERATION_ERROR: " + type(exc).__name__
                    + "; no opening call is shown and no probability is invented",
                }
            if getattr(self, "phone_alerts", None) is not None and state["mode"] == "PUBLIC MARKET DATA":
                try:
                    self.phone_alerts.consider(opening, stale=bool(state.get("stale")), now=utcnow())
                except Exception as exc:
                    self.runtime_meter.error("phone_alerts", type(exc).__name__)
            state["prediction_engine"] = engine_view(
                state, prior, opening, utcnow() if state["mode"] == "PUBLIC MARKET DATA" else now
            )
            state["model_market"] = model_market(
                state, self.estimator.slippage, self.estimator.manual_delay
            )
            state["edge_scanner"] = calculate_edge_scanner(
                state,
                slippage=self.estimator.slippage,
                manual_delay=self.estimator.manual_delay,
                now=now,
            )
            state["generated_at"] = (
                utcnow().isoformat() if state["mode"] == "PUBLIC MARKET DATA" else now.isoformat()
            )
            state["scalping_monitor"] = diagnose_scalp(state, self.scalp_criteria, now=now)
            state["monitor_alerts"] = self.monitoring_alerts.update(state)
            phases["prediction_calculation"] = round(
                (perf_counter() - calculation_started) * 1000, 2
            )
            key = (
                (state["market"]["ticker"], state["scalp"])
                if state["market"] and state["scalp"] != "WAIT" and not state["stale"]
                else None
            )
            if key != self.signal_key:
                self.signal_key = key
                self.signal_id = str(uuid.uuid4()) if key else None
            if key:
                expires = min(
                    timestamp(state["market"]["observed_at"]) + timedelta(seconds=5),
                    timestamp(state["market"]["closes_at"])
                    - timedelta(seconds=self.horizon + self.estimator.manual_delay),
                )
                state["signal"] = {
                    "id": self.signal_id,
                    "timestamp": now.isoformat(),
                    "expires_at": expires.isoformat(),
                }
            private_started = perf_counter()
            self.update_account(now)
            self.update_manual(state, now)
            phases["account_manual_scheduling"] = round(
                (perf_counter() - private_started) * 1000, 2
            )
            journal_started = perf_counter()
            self.journal.record_feeds(payload.get("coverage", {}))
            state["trade_decision"] = trade_decision(state)
            state["news_intelligence"] = classify_news(
                payload.get("coverage", {}).get("Federal Reserve public releases", {}), now
            )
            state["decision_intelligence"] = coordinate_intelligence(state)
            state["dual_strategy"] = evaluate_dual_strategy(
                state, paper_performance=self.journal.strategy_performance(state["now"])
            )
            self.journal.record_ai_signal_markers(state)
            self.journal.step(state)
            phases["journal_write"] = round((perf_counter() - journal_started) * 1000, 2)
            if isinstance(self.feed, PublicFeed):
                self.update_maintenance(now)
            else:
                for ticker, result in payload.get("settlements", {}).items():
                    self.journal.settle(ticker, result, state["mode"])
        except Exception as exc:
            self.runtime_meter.error("prediction", type(exc).__name__)
            # Keep last context for diagnosis, but never keep a usable signal.
            with self.lock:
                state = {
                    **self.state,
                    "stale": True,
                    "direction": "UNCLEAR",
                    "scalp": "WAIT",
                    "plans": {},
                    "explanation": "MISSING_DATA: public feed failed; see access diagnostic",
                    "scalp_explanation": "MISSING_DATA: public feed failed; no valid quote input",
                    "estimate": asdict(Display("WAIT", "Data unavailable; no usable estimate")),
                    "warnings": [f"Market-data failure ({type(exc).__name__}); retrying."],
                }
            codes = re.findall(r"\b[45][0-9]{2}\b", str(exc))
            if codes:
                state["warnings"].append(
                    "Public market-data access failed (HTTP "
                    + codes[0]
                    + "). Check egress/service access; no trading credentials are used."
                )
            # Account reconciliation and held-contract monitoring continue even if
            # discovery/the main public contract is temporarily unavailable.
            try:
                self.update_account(utcnow())
                self.update_manual(state, utcnow())
            except Exception:
                self.account_connected = False
            state["connection_error"] = True
            state["checked_at"] = utcnow().isoformat()
        with self.lock:
            self.state_version += 1
            published_at = utcnow()
            processing_ms = (perf_counter() - tick_started) * 1000
            for stage in (
                "market_collection", "advanced_analysis", "prediction_calculation",
                "account_manual_scheduling", "journal_write",
            ):
                if stage in phases:
                    self.runtime_meter.observe(stage, phases[stage])
            self.runtime_meter.observe("total_processing", processing_ms)
            if "collection_completed" in locals():
                self.runtime_meter.observe(
                    "publish_after_collection",
                    max(0, (published_at - collection_completed).total_seconds() * 1000),
                )
            state["runtime"] = {
                "state_version": self.state_version,
                "collection_started_at": collection_started.isoformat()
                if "collection_started" in locals()
                else None,
                "collection_completed_at": collection_completed.isoformat()
                if "collection_completed" in locals()
                else None,
                "published_at": published_at.isoformat(),
                "phases_ms": phases,
                "processing_ms": round(processing_ms, 2),
                "update_interval_ms": round((tick_started - self.last_cycle_started) * 1000, 2)
                if self.last_cycle_started is not None
                else None,
                "settlement_job_running": bool(
                    self.maintenance_job and not self.maintenance_job.done()
                ),
                "settlement_status": self.maintenance_error,
                "resources": self.runtime_meter.sample(),
                "latency_percentiles": self.runtime_meter.latency_summary(),
                "latency_scope": (
                    "Bounded recent in-process and public-request durations; "
                    "not measured one-way exchange latency"
                ),
            }
            self.state = state
            self.last_tick = published_at
            self.last_cycle_started = tick_started
            self.lock.notify_all()

    def wait_for_version(self, after, timeout):
        """Long-poll a small version token; never runs model/database work."""
        with self.lock:
            self.lock.wait_for(lambda: self.state_version > after, timeout=timeout)
            return self.state_version, self.last_tick

    def authenticate_candidate(self):
        permissions = self.account_candidate.permissions()
        self.startup_diagnostics = permissions
        if not permissions.get("read_only_verified"):
            raise AccountReadError(
                "Account monitoring disabled: "
                + str(permissions.get("blocker"))
                + "; absence of trading/transfer permission is not verified"
            )
        self.account_candidate.require_readonly = True
        batch = self.account_candidate.snapshot()
        self.account_journal.bind(
            self.account_candidate.credential_binding,  # gitleaks:allow - one-way journal binding digest
            allow_empty_rotation=True,
        )
        Path(self.account_journal.path).chmod(0o600)
        self.account_journal.reconcile(batch)
        return batch

    def sync_account(self, since):
        batch = self.account_client.snapshot(since)
        self.account_journal.reconcile(batch)
        return batch

    def update_account(self, now):
        if self.account_client is None:
            if self.account_candidate is None:
                return
            if self.account_job and self.account_job.done():
                try:
                    batch = self.account_job.result()
                    self.account_client = self.account_candidate
                    self.account_connected = True
                    self.account_error = None
                except AccountReadError as exc:
                    self.account_error = str(exc)
                except Exception:
                    self.account_error = "Account reconciliation failed; response details redacted"
                self.account_job = None
            if self.account_client is None:
                if self.account_job is None and (
                    self.account_started is None
                    or (now - self.account_started).total_seconds() >= 60
                ):
                    self.account_started = now
                    self.account_job = self.account_pool.submit(self.authenticate_candidate)
                return
        if (
            isinstance(self.feed, PublicFeed)
            and self.feed.kalshi_stream is None
            and self.account_client is not None
            and hasattr(self.account_client, "websocket_headers")
        ):
            from .kalshi_market_stream import KalshiMarketStream

            self.feed.kalshi_stream = KalshiMarketStream(self.account_client)
        if self.account_job and self.account_job.done():
            try:
                batch = self.account_job.result()
                self.account_connected = True
                self.account_error = None
            except Exception as exc:
                self.account_connected = False
                self.account_error = (
                    str(exc)
                    if isinstance(exc, AccountReadError)
                    else "Account reconciliation unavailable; private details redacted"
                )
            self.account_job = None
        if self.account_job is None and (
            self.account_started is None or (now - self.account_started).total_seconds() >= 5
        ):
            since = None
            if self.account_connected:
                recent = self.account_journal.snapshot().get("recent_fills", [])
                if recent:
                    since = max(0, int(max(timestamp(f["at"]).timestamp() for f in recent)) - 60)
            self.account_started = now
            self.account_job = self.account_pool.submit(self.sync_account, since)

    def account_view(self, state, now):
        if self.account_client is None:
            configured = credential_status()
            present = configured["KALSHI_MONITOR_KEY_ID"] and (
                configured["KALSHI_MONITOR_PRIVATE_KEY_PEM"]
                or configured["KALSHI_MONITOR_PRIVATE_KEY_PATH"]
            )
            if self.account_candidate is not None:
                status = "ACCOUNT_CONNECTING" if self.account_job is not None else "ACCOUNT_BLOCKED"
                reason = self.account_error or "Verifying authenticated read-only access"
            elif self.account_requested:
                status = "ACCOUNT_BLOCKED" if present else "ACCOUNT_NOT_CONFIGURED"
                reason = (
                    self.account_error
                    or "This process did not receive both monitoring secret settings; reopen Codespace and restart"
                )
            elif any(configured.values()) and not present:
                status = "ACCOUNT_NOT_CONFIGURED"
                reason = "Only part of the monitoring configuration reached this process; reopen Codespace and restart. See boolean secret-presence diagnostics."
            elif present:
                status = "ACCOUNT_NOT_ENABLED"
                reason = "Monitoring secrets are present, but this process was launched without account monitoring. Use scripts/start_dashboard.py --restart --private-dashboard"
            else:
                status = "PUBLIC_ONLY"
                reason = (
                    self.account_error
                    or "Public-data mode; this process has no configured account connection"
                )
            return {
                "status": status,
                "reason": reason,
                "holdings": [],
                "guidance": [],
                "permissions": self.startup_diagnostics,
            }
        account = self.account_journal.snapshot()
        cached_permissions = getattr(self.account_client, "_permissions_cache", None)
        if cached_permissions is not None:
            account["permissions"] = dict(cached_permissions)
        fresh = (
            self.account_connected
            and account.get("observed_at")
            and 0 <= (now - timestamp(account["observed_at"])).total_seconds() <= 20
        )
        balance = account.get("balance", {})
        raw_balance = balance.get("balance_dollars")
        account["available_balance_dollars"] = (
            float(raw_balance)
            if raw_balance is not None
            else float(balance["balance"]) / 100
            if balance.get("balance") is not None
            else None
        )
        account[
            "balance_note"
        ] = "Broker cash balance; pending-order reserves/withdrawable cash may differ. Last-known when stale."
        account["status"] = "READ_ONLY_CONNECTED" if fresh else "ACCOUNT_STALE"
        reconciliation = account.get("fill_reconciliation", {})
        account["reconciliation_warning"] = (
            (
                str(reconciliation.get("quarantined_fills", 0))
                + " quarantined fills; affected entry cost/P&L withheld. "
                + str(reconciliation.get("canonical_legacy_discrepancies", 0))
                + " canonical/legacy direction discrepancies audited."
            )
            if reconciliation.get("quarantined_fills")
            or reconciliation.get("canonical_legacy_discrepancies")
            else None
        )
        account["reason"] = self.account_error or (
            "Confirmed read-only account snapshot"
            if fresh
            else "Waiting for fresh startup/reconnect reconciliation"
        )
        confirmations = {}
        active_holding_ids = set()
        for holding in account.get("holdings", []):
            if (
                holding.get("status") != "OPEN"
                or not holding.get("selected")
                or not str(holding.get("ticker", "")).startswith("KXBTC15M-")
            ):
                continue
            identifier = holding["id"]
            active_holding_ids.add(identifier)
            assessment = position_forecast_assessment(holding, state, now)
            if assessment["assessment"] != "ROBUST_ADVERSE":
                self.account_forecast_candidates.pop(identifier, None)
                confirmations[str(identifier)] = False
                continue
            update_key = assessment.get("updated_at")
            candidate = self.account_forecast_candidates.get(identifier)
            if candidate is None:
                candidate = {"first_at": now, "last_update": None, "updates": 0}
                self.account_forecast_candidates[identifier] = candidate
            if update_key and update_key != candidate["last_update"]:
                candidate["last_update"] = update_key
                candidate["updates"] += 1
            confirmations[str(identifier)] = bool(
                candidate["updates"] >= 3
                and (now - candidate["first_at"]).total_seconds() >= 2
            )
        for identifier in set(self.account_forecast_candidates) - active_holding_ids:
            self.account_forecast_candidates.pop(identifier, None)
        guidance_state = {
            **state,
            "account_forecast_confirmation": confirmations,
        }
        account["guidance"] = [
            account_guidance(
                h,
                account,
                self.manual_quotes.get(h["ticker"]),
                guidance_state,
                now,
                self.manual_defaults,
                bool(fresh),
            )
            for h in account.get("holdings", [])
            if h["status"] == "OPEN" and h["selected"] and h["ticker"].startswith("KXBTC15M-")
        ]
        for guidance in account["guidance"]:
            guidance["alert_id"] = self.account_journal.alert(guidance, now)
        account["live_trade_cards"] = live_trade_cards(
            account, self.manual_quotes, guidance_state, now, self.manual_defaults, bool(fresh)
        )
        return account

    def update_manual(self, state, now):
        market = state.get("market")
        if market and not state.get("stale"):
            self.manual_quotes[market["ticker"]] = market
        for ticker, job in list(self.manual_jobs.items()):
            if job.done():
                try:
                    self.manual_quotes[ticker] = job.result()
                except Exception:
                    pass  # Old quotes naturally expire; never refresh failed data.
                del self.manual_jobs[ticker]
        if isinstance(self.feed, PublicFeed):
            for entry in self.manual.records(open_only=True)["entries"]:
                ticker = entry["ticker"]
                if (
                    entry["status"] == "OPEN"
                    and (not market or ticker != market["ticker"] or state.get("stale"))
                    and ticker not in self.manual_jobs
                ):
                    self.manual_jobs[ticker] = self.manual_pool.submit(
                        self.feed.contract_quote, ticker
                    )
        if self.account_client is not None:
            for holding in self.account_journal.snapshot().get("holdings", []):
                ticker = holding["ticker"]
                if (
                    holding["status"] == "OPEN"
                    and ticker.startswith("KXBTC15M-")
                    and (not market or ticker != market["ticker"] or state.get("stale"))
                    and ticker not in self.manual_jobs
                    and isinstance(self.feed, PublicFeed)
                ):
                    self.manual_jobs[ticker] = self.manual_pool.submit(
                        self.feed.contract_quote, ticker
                    )
        if isinstance(self.feed, PublicFeed) and self.feed.kalshi_stream is not None:
            tickers = (
                ([market["ticker"]] if market else [])
                + [
                    h["ticker"]
                    for h in self.account_journal.snapshot().get("holdings", [])
                    if h["status"] == "OPEN" and h["ticker"].startswith("KXBTC15M-")
                ]
                + [e["ticker"] for e in self.manual.records(open_only=True)["entries"]]
            )
            self.feed.kalshi_stream.set_tickers(list(dict.fromkeys(tickers))[:24])
            for ticker, quote in list(self.manual_quotes.items()):
                book = self.feed.kalshi_stream.snapshot(ticker, now)
                if book.get("status") == "LIVE":
                    self.manual_quotes[ticker] = {
                        **quote,
                        "quotes": book["quotes"],
                        "observed_at": book["observed_at"],
                        "quote_event_time": book.get("event_time"),
                        "book_diagnostic": None,
                    }
        self.manual.guidance(self.manual_quotes, now, state)

    def readiness_view(self):
        """The /readyz inputs of snapshot(), without the dashboard extras.

        /readyz used to build a full dashboard snapshot (account view, manual
        guidance, paper-strategy summary, edge scanner, ...) only to read three
        values; under readiness probing and open dashboards that work competed
        with the market-data path on a single CPU. The decision here applies the
        SAME rules as snapshot(): stale state, the 0..5 s snapshot age and
        unexpired-contract check, and diagnose_market's 5-second quote and
        reference freshness, using one clock. Nothing is relaxed.
        """
        with self.lock:
            state = dict(self.state)
            last_tick = self.last_tick
        clock = (
            utcnow()
            if state.get("mode") == "PUBLIC MARKET DATA"
            else timestamp(state["now"]) + (utcnow() - last_tick)
        )
        stale = bool(state.get("stale", True))
        market = state.get("market")
        if market:
            age = (clock - timestamp(market["observed_at"])).total_seconds()
            remaining = max(0, (timestamp(market["closes_at"]) - clock).total_seconds())
            if not 0 <= age <= 5 or not remaining:
                stale = True
        health = diagnose_market(state, clock)
        if not health["contract_quotes_fresh"] or not health["reference_fresh"]:
            stale = True
        return {"stale": stale, "market": market, "market_health": health}

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
            last_tick = self.last_tick
        market = state.get("market")
        if market:
            elapsed = utcnow() - last_tick
            clock = (
                utcnow()
                if state.get("mode") == "PUBLIC MARKET DATA"
                else timestamp(state["now"]) + elapsed
            )
            age = (clock - timestamp(market["observed_at"])).total_seconds()
            state["remaining_seconds"] = max(
                0, (timestamp(market["closes_at"]) - clock).total_seconds()
            )
            if not 0 <= age <= 5 or not state["remaining_seconds"]:
                state.update(
                    stale=True,
                    direction="UNCLEAR",
                    scalp="WAIT",
                    plans={},
                    explanation="STALE_DATA: snapshot older than 5s or contract expired",
                    scalp_explanation="STALE_DATA: snapshot older than 5s or contract expired",
                    estimate=asdict(Display("WAIT", "Stale snapshot or expired contract")),
                )
        state["manual_guidance"] = self.manual.guidance(self.manual_quotes, utcnow(), state)
        state["manual_defaults"] = self.manual_defaults
        state["account"] = self.account_view(state, utcnow())
        state["monitor_alerts"] = list(state.get("monitor_alerts", [])) + risk_alerts(
            state["account"], utcnow()
        )
        if state.get("signal") and utcnow() > timestamp(state["signal"]["expires_at"]):
            state.update(
                scalp="WAIT",
                signal=None,
                scalp_explanation="EXPIRED_SIGNAL: waiting for a fresh qualifying setup",
            )
        health_clock = (
            utcnow()
            if state.get("mode") == "PUBLIC MARKET DATA"
            else timestamp(state["now"]) + (utcnow() - last_tick)
        )
        state["market_health"] = diagnose_market(state, health_clock)
        if state.get("advanced_intelligence"):
            state["advanced_intelligence"] = refresh_status(
                state["advanced_intelligence"], health_clock
            )
        if (
            not state["market_health"]["contract_quotes_fresh"]
            or not state["market_health"]["reference_fresh"]
        ):
            state.update(
                stale=True,
                direction="UNCLEAR",
                scalp="WAIT",
                plans={},
                estimate=asdict(Display("WAIT", "Stale or missing active-contract quotes")),
                explanation="STALE_DATA: "
                + "; ".join(
                    state["market_health"]["contract_quote_reasons"]
                    + state["market_health"]["reference_reasons"]
                ),
            )
        if state.get("stale") and state.get("prediction_engine"):
            state["prediction_engine"] = {
                **state["prediction_engine"],
                "status": "STALE_LAST_KNOWN"
                if state["prediction_engine"].get("up") is not None
                else "INSUFFICIENT DATA",
                "reason": "Last valid forecast retained for display; fresh unexpired contract/reference data required before edge or trade qualification",
            }
        state["trade_decision"] = trade_decision(state)
        state["model_market"] = model_market(
            state, self.estimator.slippage, self.estimator.manual_delay
        )
        state["edge_scanner"] = calculate_edge_scanner(
            state,
            slippage=self.estimator.slippage,
            manual_delay=self.estimator.manual_delay,
            account=state["account"],
            now=health_clock,
        )
        state["decision_intelligence"] = coordinate_intelligence(state)
        reference = state.get("reference", {})
        market = state.get("market") or {}
        stream_public = state.get("streaming", {}).get("public", {})
        state["feed_health"] = {
            "status": "LIVE" if not state.get("stale") else "STALE",
            "kalshi_connection": "CONNECTED"
            if state["market_health"].get("contract_quotes_fresh") else "DISCONNECTED",
            "btc_connection": "CONNECTED"
            if state["market_health"].get("reference_fresh") else "DISCONNECTED",
            "last_successful_btc_price_update": reference.get("observed_at"),
            "last_kalshi_market_update": market.get("observed_at"),
            "btc_source": reference.get("source"),
            "btc_transport": reference.get("transport", "public REST fallback"),
            "btc_age_seconds": state["market_health"].get("reference_age_seconds"),
            "kalshi_age_seconds": state["market_health"].get("quote_age_seconds"),
            "coinbase_websocket_status": (
                "LIVE"
                if reference.get("transport") == "public WebSocket"
                and "Coinbase" in reference.get("source", "")
                and not state.get("stale")
                else "RECONNECTING_OR_FALLBACK"
                if stream_public.get("connection_attempts")
                else "UNAVAILABLE"
            ),
            "websocket_connection_attempts": stream_public.get("connection_attempts"),
            "websocket_reconnects": stream_public.get("reconnects"),
            "websocket_last_heartbeat": stream_public.get("last_heartbeat_received_at"),
            "websocket_last_error_type": stream_public.get("last_error"),
            "rest_status": state.get("streaming", {}).get("rest", {}).get("status"),
            "rest_root_failure": state.get("streaming", {}).get("rest", {}).get(
                "root_failure"
            ),
            "prediction_engine_status": state.get("prediction_engine", {}).get("status"),
            "prediction_unavailable_reason": state.get("prediction_engine", {}).get("reason"),
            "opening_status": state.get("prediction_engine", {}).get("initial", {}).get("status"),
            "opening_reason": state.get("prediction_engine", {}).get("initial", {}).get("reason"),
            "last_processing_completed_at": last_tick.isoformat(),
            "database": "WAL / serialized writes; responsive snapshot completed",
            "historical_preload": state.get("historical_preload", {}),
            "multi_asset_discovery": state.get("multi_asset_discovery", {}),
            "storage": self.journal.storage_health(),
            "runtime_failures": self.runtime_meter.failures(),
            "journal_index_maintenance": self.index_maintenance,
            "workers": self.workers_health(health_clock),
            "stale_reasons": (
                state["market_health"].get("contract_quote_reasons", [])
                + state["market_health"].get("reference_reasons", [])
            ),
        }
        state["scalping_monitor"] = diagnose_scalp(
            state, self.scalp_criteria, account=state["account"], now=health_clock
        )
        state["storage_health"] = self.journal.storage_health()
        state["dual_strategy"] = evaluate_dual_strategy(
            state, paper_performance=self.journal.strategy_performance(health_clock)
        )
        state["connection_diagnostics"] = {
            "credentials": credential_status(),
            "account_enabled": self.account_client is not None,
            "account_requested": self.account_requested,
            "account_status": state["account"]["status"],
            "account_reason": state["account"].get("reason"),
            "permissions": state["account"].get("permissions") or self.startup_diagnostics,
            "market_stale": state.get("stale", True),
            "market_reason": state.get("explanation"),
            "market_health": state["market_health"],
            "historical_preload": state.get("historical_preload", {}),
            "multi_asset_discovery": state.get("multi_asset_discovery", {}),
            "storage": self.journal.storage_health(),
            "runtime_failures": self.runtime_meter.failures(),
            "journal_index_maintenance": self.index_maintenance,
            "last_poll_completed_at": last_tick.isoformat(),
            "fill_reconciliation": state["account"].get("fill_reconciliation", {}),
            "open_positions": sum(
                h.get("status") == "OPEN" for h in state["account"].get("holdings", [])
            ),
            "balance_available": state["account"].get("available_balance_dollars") is not None,
            "reference_received_at": state.get("reference", {}).get("observed_at"),
            "quote_received_at": (state.get("market") or {}).get("observed_at"),
            "public_requests": list(getattr(self.feed, "transport_diagnostics", [])),
            "streaming": state.get("streaming", {}),
        }
        state["runtime"] = {
            **state.get("runtime", {}),
            "build_commit": self.build_commit,
            "build_short": self.build_commit[:7],
            "response_generated_at": utcnow().isoformat(),
            "state_age_seconds": max(0, (utcnow() - last_tick).total_seconds()),
        }
        state["background_learning"] = self.background_learning_status()
        return state


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Client closed a tab/request; not a failed market/account sync.

        def do_GET(self):
            request_started = perf_counter()
            parts = urlsplit(self.path)
            route = parts.path
            query = parse_qs(parts.query)
            status_code = 200
            if route == "/":
                body = Path(__file__).with_name("dashboard.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif route == "/api/state-version":
                try:
                    after = int(query.get("after", ["0"])[0])
                    timeout = float(query.get("timeout", ["15"])[0])
                    if after < 0 or after > 10**12 or not 1 <= timeout <= 20:
                        raise ValueError("Invalid state-version query")
                except (ValueError, TypeError):
                    self.send_error(400, "Use after>=0 and timeout 1..20 seconds")
                    return
                version, published_at = monitor.wait_for_version(after, timeout)
                body = json.dumps(
                    {
                        "state_version": version,
                        "changed": version > after,
                        "published_at": published_at.isoformat(),
                    }
                ).encode()
                content_type = "application/json"
            elif route in {"/api/state", "/api/details"}:
                value = monitor.snapshot()
                if route == "/api/state" and query.get("view") == ["main"]:
                    # Large raw venue books/trade histories are already journaled;
                    # retrieve them only while Details is open.
                    value["model"] = {
                        k: v
                        for k, v in value.get("model", {}).items()
                        if k in {
                            "version", "enabled", "validated", "overall_reason",
                            "scalp_reason", "developing_candle",
                        }
                    }
                    value["scalping_monitor"] = {
                        k: v for k, v in value.get("scalping_monitor", {}).items()
                        if k not in {"blockers", "checks"}
                    }
                    value.pop("coverage", None)
                    value.pop("advanced_intelligence", None)
                    value.pop("connection_diagnostics", None)
                    value.pop("streaming", None)
                    value.pop("transport_diagnostics", None)
                elif route == "/api/details" and value.get("advanced_intelligence"):
                    value["advanced_intelligence"] = attach_reliability(
                        value["advanced_intelligence"], monitor.engine_report.get()
                    )
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/live-chart":
                try:
                    limit = int(query.get("limit", ["180"])[0])
                    if not 30 <= limit <= 500:
                        raise ValueError("Invalid live chart limit")
                except (ValueError, TypeError):
                    self.send_error(400, "Use limit 30..500")
                    return
                value = monitor.live_trade_chart.snapshot(utcnow(), limit)
                with monitor.lock:
                    websocket_status = (
                        (monitor.state.get("feed_health") or {}).get(
                            "coinbase_websocket_status"
                        )
                    )
                value["connection_status"] = websocket_status or "UNAVAILABLE"
                if value["status"] == "NO_RECORDED_TRADES" and websocket_status == "LIVE":
                    value["status"] = "CONNECTED_IDLE"
                    value["reason"] = "WebSocket connected; waiting for the next genuine BTC trade"
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/signal-chart":
                intervals = {"15s": 15, "1m": 60, "3m": 180, "5m": 300, "15m": 900}
                try:
                    interval_name = query.get("interval", ["1m"])[0]
                    interval_seconds = intervals[interval_name]
                    limit = int(query.get("limit", ["240"])[0])
                    ticker = query.get("ticker", [None])[0]
                    if not 30 <= limit <= 500:
                        raise ValueError("Invalid chart limit")
                    if ticker is not None and not re.fullmatch(
                        r"KXBTC15M-[A-Za-z0-9_-]{1,100}", ticker
                    ):
                        raise ValueError("Invalid chart ticker")
                except (KeyError, ValueError, TypeError):
                    self.send_error(400, "Use interval 15s/1m/3m/5m/15m and limit 30..500")
                    return
                if interval_seconds == 15:
                    # Marker/performance history remains journal-backed, while
                    # genuine sub-minute OHLCV comes only from timestamped
                    # Coinbase market_trades WebSocket executions.
                    value = monitor.journal.signal_chart(
                        60,
                        limit,
                        ticker,
                        candle_fallback=(
                            monitor.latest_candles
                            or getattr(monitor.feed, "candle_cache", None)
                        ),
                    )
                    live = monitor.live_trade_chart.snapshot(utcnow(), limit)
                    value.update(live)
                    with monitor.lock:
                        websocket_status = (
                            (monitor.state.get("feed_health") or {}).get(
                                "coinbase_websocket_status"
                            )
                        )
                    value["connection_status"] = websocket_status or "UNAVAILABLE"
                    if value["status"] == "NO_RECORDED_TRADES" and websocket_status == "LIVE":
                        value["status"] = "CONNECTED_IDLE"
                        value["reason"] = "WebSocket connected; waiting for the next genuine BTC trade"
                    else:
                        value["reason"] = None if value["status"] == "READY" else value.get("reason")
                else:
                    value = monitor.journal.signal_chart(
                        interval_seconds,
                        limit,
                        ticker,
                        candle_fallback=(
                            monitor.latest_candles
                            or getattr(monitor.feed, "candle_cache", None)
                        ),
                    )
                with monitor.lock:
                    developing = dict(
                        ((monitor.state.get("model") or {}).get("developing_candle") or {})
                    )
                if interval_seconds != 15:
                    value["forming_candle"] = None
                if interval_seconds != 15 and developing.get("status") == "DEVELOPING" and value.get("status") == "READY":
                    try:
                        start = int(developing["start"])
                        bucket = start - start % interval_seconds
                        forming = {
                            **developing,
                            "start": bucket,
                            "interval_seconds": interval_seconds,
                        }
                        # A larger-timeframe aggregate can contain completed
                        # one-minute source bars from its still-open bucket.
                        # Move that partial aggregate into the faded developing
                        # candle so it is never presented twice or called closed.
                        bars = value.get("bars") or []
                        if bars and bars[-1].get("start") == bucket:
                            partial = bars.pop()
                            forming.update(
                                open=partial["open"],
                                high=max(float(partial["high"]), float(developing["high"])),
                                low=min(float(partial["low"]), float(developing["low"])),
                                close=float(developing["close"]),
                                volume=partial.get("volume"),
                                source_bars=partial.get("source_bars", 0),
                            )
                        value["forming_candle"] = forming
                    except (KeyError, TypeError, ValueError, OverflowError):
                        value["forming_candle"] = None
                latest = value.get("latest_bar_start")
                if value.get("bars"):
                    latest = value["bars"][-1]["start"]
                    value["latest_bar_start"] = latest
                value["latest_closed_candle_age_seconds"] = (
                    max(0, utcnow().timestamp() - latest - interval_seconds)
                    if isinstance(latest, (int, float))
                    else None
                )
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/candle-integrity":
                # Copy one atomic publication. Never combine bars from one
                # collector cycle with diagnostics from another.
                with monitor.candle_lock:
                    source = deepcopy(
                        monitor.latest_candles
                        or getattr(monitor.feed, "candle_cache", None)
                        or {}
                    )
                body = json.dumps(
                    candle_integrity_report(source, utcnow()), allow_nan=False
                ).encode()
                content_type = "application/json"
            elif route == "/api/data-quality-alerts":
                # Read-only Phase 5 monitoring; never changes a forecast and is
                # independent of /healthz and /readyz.
                from .opening_alerts import compute_alerts

                wall = utcnow()
                with monitor.candle_lock:
                    source = deepcopy(
                        monitor.latest_candles
                        or getattr(monitor.feed, "candle_cache", None)
                        or {}
                    )
                ages = {
                    name: (wall - history[-1][0]).total_seconds()
                    for name, history in list(monitor.estimator.opening_references.items())
                    if history
                }
                artifact = getattr(monitor, "opening_artifact", None) or {}
                model = artifact.get("model") or {}
                inputs = sorted({
                    name for part in (model.get("components") or [model])
                    for name in ((part.get("preprocessor") or {}).get("names") or [])
                })
                try:
                    recent = monitor.journal.recent_openings("PUBLIC MARKET DATA")
                except sqlite3.OperationalError:
                    recent = []
                body = json.dumps(
                    compute_alerts(recent, candle_integrity_report(source, wall), ages, inputs, wall),
                    allow_nan=False, default=str,
                ).encode()
                content_type = "application/json"
            elif route == "/api/market-history":
                try:
                    before_close_value = query.get("before_close", [None])[0]
                    before_close = (
                        "" if before_close_value == "null" else before_close_value
                    )
                    before_ticker = query.get("before_ticker", [None])[0]
                    limit = int(query.get("limit", ["20"])[0])
                    if not 1 <= limit <= 100:
                        raise ValueError("Invalid page size")
                    if (before_close is None) != (before_ticker is None):
                        raise ValueError("Incomplete market history cursor")
                    if before_ticker is not None and not re.fullmatch(
                        r"KXBTC15M-[A-Za-z0-9_-]{1,100}", before_ticker
                    ):
                        raise ValueError("Invalid market history cursor")
                    if before_close:
                        timestamp(before_close)
                except (ValueError, TypeError):
                    self.send_error(
                        400,
                        "Use limit 1..100 and a matching before_close/before_ticker cursor",
                    )
                    return
                value = monitor.journal.market_history_page(
                    before_close=before_close,
                    before_ticker=before_ticker,
                    limit=limit,
                    now=utcnow(),
                )
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route in {"/api/history-page", "/api/account-history"}:
                if route == "/api/account-history" and monitor.account_client is None:
                    self.send_error(
                        403, "Authenticated read-only account monitoring is not enabled"
                    )
                    return
                try:
                    before = int(query.get("before_id", ["0"])[0])
                    limit = int(query.get("limit", ["20"])[0])
                    if before < 0 or not 1 <= limit <= 100:
                        raise ValueError("Invalid page")
                except (ValueError, TypeError):
                    self.send_error(400, "Use nonnegative before_id and limit 1..100")
                    return
                value = (
                    monitor.journal.history_page(before_id=before, limit=limit)
                    if route == "/api/history-page"
                    else monitor.account_journal.completed_history(
                        before=before or None, limit=limit
                    )
                )
                if route == "/api/history-page":
                    value["manual_records"] = monitor.manual.records(
                        limit=limit, before_id=before or None
                    )
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/scalp-criteria":
                body = json.dumps(
                    {
                        "criteria": monitor.scalp_criteria.to_dict(),
                        "notice": "Diagnostic criteria only; no evidence or account limits changed",
                    }
                ).encode()
                content_type = "application/json"
            elif route == "/api/scalp-paper-performance":
                report = monitor.scalp_paper_report.get()
                body = json.dumps(
                    {
                        "status": report["status"],
                        "report": report["value"],
                        "generated_at": report["generated_at"],
                        "calculation_ms": report["calculation_ms"],
                        "notice": "Public quote counterfactuals only, never actual account results or validated trade recommendations",
                    },
                    allow_nan=False,
                ).encode()
                content_type = "application/json"
            elif route in {"/api/history", "/api/research"}:
                report = monitor.research_report.get()
                value = {
                    "status": report["status"],
                    "performance": report["value"],
                    "generated_at": report["generated_at"],
                    "calculation_ms": report["calculation_ms"],
                    "refresh_in_progress": report["refresh_in_progress"],
                    "notice": "Research is calculated in the background. Use /api/history-page for bounded records.",
                }
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/prediction-performance":
                report = monitor.engine_report.get()
                value = {
                    **(report["value"] or {"by_mode": {}, "archive": []}),
                    "report_status": report["status"],
                    "report_generated_at": report["generated_at"],
                    "calculation_ms": report["calculation_ms"],
                    "refresh_in_progress": report["refresh_in_progress"],
                }
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/edge-performance":
                report = monitor.edge_report.get()
                value = {
                    "status": report["status"],
                    "performance": report["value"],
                    "generated_at": report["generated_at"],
                    "calculation_ms": report["calculation_ms"],
                    "refresh_in_progress": report["refresh_in_progress"],
                    "notice": "Counterfactual edge observations are separate from actual account results and never imply real fills",
                }
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/api/opening-scoreboard":
                report = monitor.opening_scoreboard_report.get()
                body = json.dumps(
                    {
                        "status": report["status"],
                        "scoreboard": report["value"],
                        "generated_at": report["generated_at"],
                    },
                    allow_nan=False,
                ).encode()
                content_type = "application/json"
            elif route == "/api/learning-summary":
                report = monitor.quick_learning_report.get()
                body = json.dumps(
                    {
                        "status": report["status"],
                        "summary": report["value"],
                        "generated_at": report["generated_at"],
                        "calculation_ms": report["calculation_ms"],
                    },
                    allow_nan=False,
                ).encode()
                content_type = "application/json"
            elif route == "/api/probability-history":
                ticker = query.get("ticker", [""])[0]
                try:
                    limit = int(query.get("limit", ["180"])[0])
                    if (
                        not re.fullmatch(r"KXBTC15M-[A-Za-z0-9_-]{1,100}", ticker)
                        or not 2 <= limit <= 180
                    ):
                        raise ValueError("Invalid bounded history query")
                except (ValueError, TypeError):
                    self.send_error(400, "A KXBTC15M ticker and limit 2..180 are required")
                    return
                from .prediction_engine import probability_history

                with monitor.journal.read() as db:
                    value = probability_history(db, ticker, limit=limit, now=utcnow())
                body = json.dumps(value, allow_nan=False).encode()
                content_type = "application/json"
            elif route == "/healthz":
                with monitor.lock:
                    age = max(0, (utcnow() - monitor.last_tick).total_seconds())
                    version = monitor.state_version
                workers = monitor.workers_health()
                healthy = version > 0 and age <= 10 and workers["healthy"]
                status_code = 200 if healthy else 503
                body = json.dumps(
                    {
                        "status": "HEALTHY" if healthy else "UNHEALTHY",
                        "state_age_seconds": round(age, 3),
                        "state_version": version,
                        "build_commit": monitor.build_commit,
                        "signals_only": True,
                        "workers": workers,
                    }
                ).encode()
                content_type = "application/json"
            elif route == "/readyz":
                state = monitor.readiness_view()
                health = state.get("market_health", {})
                ready = bool(
                    not state.get("stale", True)
                    and health.get("contract_quotes_fresh")
                    and health.get("reference_fresh")
                    and state.get("market")
                )
                status_code = 200 if ready else 503
                body = json.dumps({
                    "status": "READY" if ready else "NOT_READY",
                    "build_commit": monitor.build_commit,
                    "signals_only": True,
                    "contract": (state.get("market") or {}).get("ticker"),
                    "contract_quotes_fresh": bool(health.get("contract_quotes_fresh")),
                    "reference_fresh": bool(health.get("reference_fresh")),
                    "stale_reasons": health.get("contract_quote_reasons", [])
                        + health.get("reference_reasons", []),
                    "note": "Readiness may fail during an external feed outage without making the dashboard process unhealthy.",
                }).encode()
                content_type = "application/json"
            elif route == "/health":
                body, content_type = b"signals-only dashboard", "text/plain"
            else:
                self.send_error(404)
                return
            request_ms = (perf_counter() - request_started) * 1000
            monitor.runtime_meter.observe(
                "http_main_state"
                if route == "/api/state" and query.get("view") == ["main"]
                else "http_chart"
                if route == "/api/signal-chart"
                else "http_other",
                request_ms,
            )
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Dashboard-Build", monitor.build_commit)
            self.send_header(
                "Server-Timing",
                "dashboard;dur=" + str(round(request_ms, 2)),
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path not in (
                "/api/manual/entry",
                "/api/manual/exit",
                "/api/account/select",
                "/api/account/limits",
                "/api/scalp-criteria",
            ):
                self.send_error(405, "No trading/write route")
                return
            origin = urlsplit(self.headers.get("Origin", ""))
            hosts = {
                self.headers.get("Host", ""),
                self.headers.get("X-Forwarded-Host", "").split(",")[0].strip(),
            }
            if (
                not origin.netloc
                or origin.netloc not in hosts
                or origin.scheme not in ("http", "https")
            ):
                self.send_error(403, "Same-origin journal requests only")
                return
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                self.send_error(415, "JSON manual records only")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid journal payload size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Journal record must be an object")
                if self.path == "/api/scalp-criteria":
                    criteria = monitor.journal.scalp_criteria(data)
                    monitor.scalp_criteria = criteria
                    result = {
                        "recorded": True,
                        "criteria": criteria.to_dict(),
                        "executed_trade": False,
                    }
                elif self.path.startswith("/api/account/"):
                    if monitor.account_client is None:
                        raise ValueError("Account mode is not enabled on the server")
                    if self.path.endswith("/select"):
                        if set(data) != {"holding_id", "enabled"}:
                            raise ValueError("Invalid selection fields")
                        monitor.account_journal.select(data["holding_id"], data["enabled"])
                    elif data == {"disable": True}:
                        with monitor.account_journal.connect() as db:
                            db.execute("DELETE FROM monitor_account_settings WHERE name='limits'")
                    else:
                        monitor.account_journal.limits(data)
                    result = {"recorded": True, "executed_trade": False}
                elif self.path.endswith("/entry"):
                    identifier = monitor.manual.enter(data, utcnow(), monitor.manual_defaults)
                    result = {"recorded": True, "entry_id": identifier, "executed_trade": False}
                else:
                    monitor.manual.exit(data, utcnow())
                    result = {"recorded": True, "executed_trade": False}
                status = 200
            except (KeyError, ValueError, TypeError) as exc:
                result = {"error": str(exc), "executed_trade": False}
                status = 422
            body = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            self.send_error(405, "No trading/write route")

        do_PATCH = do_DELETE = do_PUT

    return Handler


class StartupStatus:
    """In-memory startup status; never touches journals, feeds or credentials."""

    def __init__(self, build_commit):
        self.build_commit = build_commit
        self.started_at = utcnow()
        self.updated_at = self.started_at
        self.stage = "WEB_READY"
        self.message = "Dashboard shell is ready; preserved data is loading"
        self.lock = threading.Lock()

    def update(self, stage, message):
        with self.lock:
            self.stage, self.message, self.updated_at = stage, message, utcnow()

    def fail(self, stage, exc):
        # Expose only the exception class.  Exception messages can contain
        # paths, proxy details, or provider responses and do not belong in the
        # browser or logs used for support.
        self.update("FAILED_" + stage, "Initialization failed: " + type(exc).__name__)

    def snapshot(self):
        with self.lock:
            return {
                "status": "STARTING",
                "stage": self.stage,
                "message": self.message,
                "started_at": self.started_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
                "build_commit": self.build_commit,
                "signals_only": True,
            }

    def dashboard_state(self):
        value = self.snapshot()
        reason = "STARTING / " + value["stage"] + ": " + value["message"]
        return {
            "mode": "STARTING", "now": value["updated_at"], "stale": True,
            "direction": "UNCLEAR", "scalp": "WAIT", "scalp_lean": "WAIT",
            "remaining_seconds": 0, "market": None, "market_strip": [],
            "multi_asset_discovery": {"status": "WARMING UP", "reason": reason},
            "reference": {},
            "estimate": {"settlement_up": None, "settlement_down": None, "reason": reason},
            "plans": {}, "warnings": [reason], "explanation": reason,
            "scalp_explanation": reason,
            "prediction_engine": {"status": "STARTING", "reason": reason,
                                  "initial": {"status": "STARTING", "up": None,
                                              "reason": reason}},
            "account": {"status": "ACCOUNT_CONNECTING", "reason": reason},
            "feed_health": {"status": "WARMING UP", "prediction_engine_status": "STARTING",
                            "prediction_unavailable_reason": reason},
            "market_health": {"contract_quotes_fresh": False, "reference_fresh": False,
                              "contract_quote_reasons": [reason],
                              "reference_reasons": [reason]},
            "runtime": {"build_commit": value["build_commit"],
                        "build_short": value["build_commit"][:7], "state_version": 0,
                        "published_at": value["updated_at"], "startup": value},
        }


def make_startup_handler(startup):
    """Serve the real dashboard while preserved journals and models load."""
    if isinstance(startup, str):
        startup = StartupStatus(startup)

    class StartupHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == "/":
                body = Path(__file__).with_name("dashboard.html").read_bytes()
                content_type, status = "text/html; charset=utf-8", 200
            elif route in {"/healthz", "/readyz", "/api/startup"}:
                body = json.dumps(startup.snapshot()).encode()
                content_type, status = "application/json", 503
            elif route in {"/api/state", "/api/details"}:
                body = json.dumps(startup.dashboard_state()).encode()
                content_type, status = "application/json", 200
            elif route == "/api/state-version":
                current = startup.snapshot()
                body = json.dumps({"state_version": 0, "changed": False,
                                   "published_at": current["updated_at"]}).encode()
                content_type, status = "application/json", 200
            elif route == "/health":
                body, content_type, status = b"signals-only dashboard starting", "text/plain", 200
            else:
                body = json.dumps({"status": "STARTING",
                                   "reason": startup.snapshot()["message"]}).encode()
                content_type, status = "application/json", 503
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Dashboard-Build", startup.build_commit)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return StartupHandler


def main():
    parser = argparse.ArgumentParser(
        description="KXBTC15M signals-only public/replay monitor with optional GET-only account monitoring"
    )
    parser.add_argument(
        "--disable-model",
        action="store_true",
        help="Quotes-only mode; panels explicitly report MODEL_DISABLED",
    )
    parser.add_argument("--mode", choices=["public", "replay"], default="replay")
    parser.add_argument("--replay", type=Path, default=Path(__file__).with_name("demo_replay.json"))
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--build-commit", default="unknown")
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument(
        "--processing-ms",
        type=int,
        default=500,
        help="Minimum interval between event-driven model updates (100..1000ms); provider cadence may be slower",
    )
    parser.add_argument("--account-store", type=Path, default=Path("signals_account_monitor.db"))
    parser.add_argument(
        "--account-readonly",
        action="store_true",
        help="Enable isolated authenticated GET-only account monitoring",
    )
    parser.add_argument(
        "--private-dashboard",
        action="store_true",
        help="Acknowledge account dashboard is private; verify Codespaces port visibility yourself",
    )
    parser.add_argument(
        "--require-readonly-scope",
        action="store_true",
        default=True,
        help="Refuse account monitoring unless authenticated key metadata verifies read-only scope",
    )
    parser.add_argument("--model-artifact", type=Path, default=Path("signals_model.json"))
    parser.add_argument(
        "--opening-model-artifact", type=Path, default=Path("signals_opening_model.json")
    )
    parser.add_argument("--model-registry", type=Path, default=Path("signals_model_registry"))
    parser.add_argument("--learning-status", type=Path)
    parser.add_argument(
        "--disable-scheduled-learning",
        action="store_true",
        help="Disable background daily archive-only model comparisons; never affects read-only account protection",
    )
    parser.add_argument("--manual-delay", type=int, default=3)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--target", type=float, default=0.05)
    parser.add_argument("--stop", type=float, default=0.10)
    parser.add_argument("--horizon", type=int, default=60)
    args = parser.parse_args()
    if not (
        0 < args.target <= 1
        and 0 < args.stop <= 1
        and 1 <= args.horizon <= 900
        and 1 <= args.poll_seconds <= 5
        and 100 <= args.processing_ms <= 1000
        and 0 <= args.manual_delay <= 30
        and 0 <= args.slippage <= 0.10
    ):
        parser.error(
            "target/stop must be in (0,1], horizon 1..900s, REST polling 1..5s, processing 100..1000ms"
        )
    if args.account_readonly and (not args.private_dashboard or args.mode != "public"):
        parser.error(
            "Account mode requires --mode public --private-dashboard and a Private forwarded port"
        )
    # Reserve port before opening/migrating journals or reading credentials.
    startup = StartupStatus(args.build_commit)
    try:
        server = ThreadingHTTPServer(
            (args.host, args.port), make_startup_handler(startup)
        )
    except OSError:
        parser.error("Dashboard port is already in use; use scripts/start_dashboard.py --restart")
    import fcntl

    guards = []
    for path in (args.journal, args.account_store) if args.account_readonly else (args.journal,):
        guard = open(str(path.resolve()) + ".dashboard.lock", "a")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            guard.close()
            server.server_close()
            parser.error("Another dashboard owns this journal; no database changes made")
        guards.append(guard)
    # Begin serving the lightweight STARTING endpoint before any database,
    # account, model, or public-feed initialization. This keeps Codespaces and
    # the supervisor observable even when preserved state takes time to open.
    server_thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="dashboard-http"
    )
    server_thread.start()
    startup.update("JOURNAL_LOCK", "Checking exclusive dashboard ownership without deleting data")
    account_candidate = None
    disabled_reason = None
    if args.account_readonly:
        try:
            account_candidate = ReadOnlyKalshi.from_environment()
            disabled_reason = "Checking production authentication and broker read-only scope; account reads remain withheld until verified"
        except AccountReadError as exc:
            disabled_reason = str(exc)
            print(disabled_reason, flush=True)
    startup_stage = "MARKET_FEEDS"
    try:
        startup.update(startup_stage, "Preparing independent public market-data workers")
        feed = PublicFeed() if args.mode == "public" else ReplayFeed(args.replay)
        startup_stage = "SIGNAL_JOURNAL"
        startup.update(startup_stage, "Opening preserved signal and learning journal")
        journal = Journal(args.journal)
        startup_stage = "INTELLIGENCE"
        startup.update(startup_stage, "Constructing the signals-only intelligence monitor")
        monitor = Monitor(
            feed,
            journal,
            target=args.target,
            stop=args.stop,
            horizon=args.horizon,
            models_enabled=not args.disable_model,
            manual_delay=args.manual_delay,
            slippage=args.slippage,
            model_artifact=args.model_artifact,
            account_client=None,
            account_store=args.account_store,
            opening_model_artifact=args.opening_model_artifact,
            build_commit=args.build_commit,
            model_registry=args.model_registry
            if args.mode == "public"
            and not args.disable_model
            and not args.disable_scheduled_learning
            else None,
            learning_status_path=args.learning_status,
            startup_update=startup.update,
        )
    except Exception as exc:
        startup.fail(startup_stage, exc)
        print("Dashboard initialization failed at " + startup_stage + ": " + type(exc).__name__, flush=True)
        # Leave the diagnostic shell reachable briefly so the launcher and the
        # user can observe the exact failed stage, then exit for supervisor
        # recovery.  Never spin forever in STARTING.
        threading.Event().wait(5)
        server.shutdown()
        server.server_close()
        raise
    monitor.account_error = disabled_reason
    monitor.account_requested = args.account_readonly
    monitor.account_candidate = account_candidate
    monitor.update_account(utcnow())
    startup.update("ENGINE_READY", "Intelligence initialized; starting live update workers")
    stop = threading.Event()
    import signal

    def terminate(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)

    mailbox = LatestPayload()

    def collect():
        # Public IO has its own thread; prediction/research/account work never
        # delays a network refresh. No unbounded queue of obsolete frames.
        last_success = None
        try:
            while not stop.is_set():
                monitor.worker_event("collector", "started")
                started = utcnow()
                timer = perf_counter()
                failure = None
                try:
                    payload = feed.poll()
                    last_success = payload
                except Exception as exc:
                    failure = exc
                    monitor.runtime_meter.error("public_rest", feed.poll_failure_code(exc))
                    payload = {
                        **(
                            last_success
                            or {
                                "markets": [],
                                "reference": {},
                                "mode": "PUBLIC MARKET DATA",
                                "coverage": {},
                            }
                        ),
                        "warnings": [
                            "Public REST refresh failed ("
                            + type(exc).__name__
                            + "); cached receipts retain their original timestamps"
                        ],
                        "rest_poll_failed": True,
                        "rest_error_type": type(exc).__name__,
                        "btc_candles": feed.sampled_candles(utcnow()),
                    }
                # Candle history is chart input, not optional model output.
                # Publish it before the prediction mailbox so a slow analysis
                # cycle cannot leave /api/signal-chart empty after preload is
                # already READY.
                monitor.accept_candle_batch(payload, utcnow())
                elapsed = perf_counter() - timer
                mailbox.put((payload, started, utcnow(), elapsed * 1000))
                monitor.worker_event("collector", "completed", failure)
                stop.wait(max(0, args.poll_seconds - elapsed))
        except BaseException as exc:
            monitor.worker_event("collector", "stopped", exc)
            raise
        finally:
            if stop.is_set():
                monitor.worker_event("collector", "stopped")

    def poll():
        sequence = 0
        previous = None
        source_key = None
        last_processed = -float("inf")
        try:
            while not stop.is_set():
                if isinstance(feed, PublicFeed):
                    sequence, collected = mailbox.get(sequence, timeout=0.01)
                    if collected is not None:
                        previous = collected
                    if previous is None:
                        stop.wait(0.01)
                        continue
                    stream_sequence = getattr(feed.streams, "sequence", 0)
                    book_sequence = getattr(feed.kalshi_stream, "sequence", 0)
                    key = (sequence, stream_sequence, book_sequence)
                    elapsed = monotonic() - last_processed
                    if elapsed < args.processing_ms / 1000 or (key == source_key and elapsed < 1):
                        stop.wait(0.01)
                        continue
                    source_key = key
                    monitor.worker_event("prediction", "started")
                    # One malformed enrichment frame must not permanently stop
                    # this thread. The next loop processes the newest mailbox
                    # value; the failed cycle remains explicitly non-actionable.
                    failure = process_prediction_frame(monitor, feed, previous)
                    monitor.worker_event("prediction", "completed", failure)
                    last_processed = monotonic()
                else:
                    monitor.worker_event("prediction", "started")
                    monitor.tick()
                    monitor.worker_event("prediction", "completed")
                    stop.wait(args.poll_seconds)
        except BaseException as exc:
            monitor.worker_event("prediction", "stopped", exc)
            raise
        finally:
            if stop.is_set():
                monitor.worker_event("prediction", "stopped")

    def maintain_indexes():
        background_thread_priority()
        try:
            result = journal.ensure_performance_indexes()
            monitor.index_maintenance = result
        except Exception as exc:
            monitor.runtime_meter.error("journal_indexes", type(exc).__name__)
            monitor.index_maintenance = {
                "status": "ERROR", "error_type": type(exc).__name__
            }

    server.RequestHandlerClass = make_handler(monitor)
    collector = (
        threading.Thread(target=collect, daemon=True) if isinstance(feed, PublicFeed) else None
    )
    if collector:
        monitor.worker_threads["collector"] = collector
        collector.start()
    thread = threading.Thread(target=poll, daemon=True)
    monitor.worker_threads["prediction"] = thread
    thread.start()
    # The first live workers are running before any potentially expensive
    # index build. SQLite WAL keeps reads/live receipts available meanwhile.
    threading.Thread(target=maintain_indexes, daemon=True, name="journal-index-maintenance").start()
    print(
        f"Signals-only {args.mode} dashboard on port {server.server_port}; journal: {args.journal}",
        flush=True,
    )
    try:
        while server_thread.is_alive():
            server_thread.join(timeout=1)
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        thread.join(timeout=35)
        if collector:
            collector.join(timeout=10)
        monitor.manual_pool.shutdown(wait=False, cancel_futures=True)
        monitor.account_pool.shutdown(wait=False, cancel_futures=True)
        monitor.maintenance_pool.shutdown(wait=False, cancel_futures=True)
        monitor.readiness_pool.shutdown(wait=False, cancel_futures=True)
        monitor.engine_report.close()
        monitor.research_report.close()
        monitor.scalp_paper_report.close()
        monitor.edge_report.close()
        monitor.quick_learning_report.close()
        monitor.opening_scoreboard_report.close()
        if monitor.scheduled_learning:
            monitor.scheduled_learning.close()
        if monitor.scheduled_scalp_learning:
            monitor.scheduled_scalp_learning.close()
        if isinstance(feed, PublicFeed):
            feed.close()


if __name__ == "__main__":
    main()

"""Read-only browser dashboard. Start manually; defaults to replay, never trading."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlsplit, parse_qs
from time import perf_counter, monotonic
import uuid

from .account_readonly import ReadOnlyKalshi, AccountReadError, credential_status
from .account_state import AccountJournal, account_guidance, live_trade_cards
from .scanner import context as scanner_context, qualify as scanner_qualify
from .decision import assess as trade_decision
from .market_health import selection_summary, diagnose as diagnose_market
from .prediction_engine import (
    view as engine_view,
    opening_forecast,
    performance as engine_performance,
)
from .manual import ManualJournal
from .live_model import LiveEstimator
from .learning import load_artifact, predict as learned_predict
from .model import Display, timestamp
from .monitor import Journal, PublicFeed, ReplayFeed, active_market, analyze, utcnow
from .runtime import CachedReport, LatestPayload, RuntimeMeter
from .intelligence import MarketIntelligence
from .comparison import model_market
from .alerts import MonitoringAlerts, risk_alerts
from .scalp_monitor import diagnose as diagnose_scalp, ScalpCriteria
from .advanced import AdvancedIntelligence, attach_reliability, refresh_status
from .edge_scanner import calculate as calculate_edge_scanner, performance as edge_performance


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
        opening_model_artifact=None,
    ):
        self.feed, self.journal = feed, journal
        self.target, self.stop, self.horizon = target, stop, horizon
        self.account_client = account_client
        self.account_candidate = None
        self.account_requested = account_client is not None
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
        self.account_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="account-read-only"
        )
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
        self.artifact, self.artifact_status = load_artifact(model_artifact)
        from .opening_models import load_artifact as load_opening_artifact

        self.opening_artifact, self.opening_artifact_status = load_opening_artifact(
            opening_model_artifact
        )
        self.scalp_criteria = self.journal.scalp_criteria()
        self.readiness_at = None
        self.readiness = {}
        self.estimator = LiveEstimator(manual_delay=manual_delay, slippage=slippage)
        if isinstance(feed, PublicFeed):
            self.estimator.restore(journal.recent_states())
        self.lock = threading.Lock()
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
        self.intelligence = MarketIntelligence()
        self.advanced = AdvancedIntelligence()
        self.monitoring_alerts = MonitoringAlerts()
        self.last_proxy_observation = None
        self.runtime_meter = RuntimeMeter()
        self.maintenance_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="settlement-results"
        )
        self.maintenance_job = None
        self.maintenance_error = None
        self.readiness_job = None
        self.readiness_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="learning-readiness"
        )
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
        self.scheduled_learning = None
        if model_registry is not None:
            from .model_registry import scheduled_evaluation

            self.scheduled_learning = CachedReport(
                lambda: scheduled_evaluation(self.journal.path, model_registry),
                interval=3600,
                name="scheduled-offline-model-comparison",
            )

    def prediction_performance(self):
        with self.journal.read() as db:
            return engine_performance(db, utcnow())

    def edge_performance(self):
        with self.journal.read() as db:
            return edge_performance(db)

    def maintain_settlements(self, now):
        self.journal.seed_settlement_queue()
        for ticker in self.journal.due_settlements(now):
            self.journal.mark_settlement_attempt(ticker, now)
            try:
                self.journal.settle(ticker, self.feed.resolved(ticker), "PUBLIC MARKET DATA")
            except Exception:
                # No stale or fabricated outcome on HTTP failure.
                self.maintenance_error = "Official settlement result pending or inaccessible"

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
            for discovered in payload.get("markets", []):
                try:
                    opened = timestamp(discovered["opens_at"])
                    if not opened <= now <= opened + timedelta(seconds=6):
                        continue
                    raw_history = payload.get("btc_candles", {})
                    try:
                        eligible = timestamp(raw_history["received_at"]) <= opened
                    except (KeyError, ValueError, TypeError, AttributeError):
                        eligible = False
                    frozen_history = raw_history if eligible else self.journal.opening_inputs(opened)
                    frozen_points, frozen_source = self.estimator.opening_points(opened)
                    self.journal.freeze_opening_snapshot(
                        discovered,
                        mode,
                        frozen_history,
                        frozen_points,
                        frozen_source,
                        now,
                        finalized=now >= opened + timedelta(seconds=5),
                    )
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
            state["market_health"] = diagnose_market(state, now)
            prior = (
                self.journal.engine_prior(state["market"]["ticker"], state["mode"])
                if state.get("market")
                else None
            )
            opening = None
            if state.get("market") and self.models_enabled:
                m = state["market"]
                opening = self.journal.opening_record(m["ticker"], state["mode"])
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
                    candidate = opening_forecast(
                        m,
                        frozen,
                        frozen_points,
                        generated,
                        reference_source=frozen_source,
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
                        },
                        frozen_context={
                            **self.intelligence.summarize(
                                opened, candles=frozen, primary_source=primary
                            ),
                            "primary_source": primary,
                            "advanced_features": self.advanced.summarize(
                                opened,
                                candles=frozen,
                                primary_source=primary,
                                threshold=m["strike"],
                            ),
                        },
                        candidate_artifact=self.opening_artifact,
                    )
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
                    retry_until = opened + timedelta(seconds=5)
                    if candidate.get("up") is not None or generated >= retry_until:
                        opening = self.journal.opening_record(
                            m["ticker"], state["mode"], candidate, generated
                        )
                    else:
                        candidate["status"] = "WARMING UP"
                        candidate["reason"] += "; retrying receipt-proven pre-opening inputs for up to 5 seconds"
                        candidate["retry_until"] = retry_until.isoformat()
                        opening = candidate
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
            self.journal.record_candles(payload.get("btc_candles", {}), now)
            state["trade_decision"] = trade_decision(state)
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
                "processing_ms": round((perf_counter() - tick_started) * 1000, 2),
                "update_interval_ms": round((tick_started - self.last_cycle_started) * 1000, 2)
                if self.last_cycle_started is not None
                else None,
                "settlement_job_running": bool(
                    self.maintenance_job and not self.maintenance_job.done()
                ),
                "settlement_status": self.maintenance_error,
                "resources": self.runtime_meter.sample(),
            }
            self.state = state
            self.last_tick = published_at
            self.last_cycle_started = tick_started

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
            self.account_candidate.credential_binding, allow_empty_rotation=True
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
        account["guidance"] = [
            account_guidance(
                h,
                account,
                self.manual_quotes.get(h["ticker"]),
                state,
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
            account, self.manual_quotes, state, now, self.manual_defaults, bool(fresh)
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
                "status": "INSUFFICIENT DATA",
                "up": None,
                "down": None,
                "reason": "Fresh unexpired contract/reference data required",
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
        reference = state.get("reference", {})
        market = state.get("market") or {}
        stream_public = state.get("streaming", {}).get("public", {})
        state["feed_health"] = {
            "status": "LIVE" if not state.get("stale") else "STALE",
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
            "prediction_engine_status": state.get("prediction_engine", {}).get("status"),
            "prediction_unavailable_reason": state.get("prediction_engine", {}).get("reason"),
            "opening_status": state.get("prediction_engine", {}).get("initial", {}).get("status"),
            "opening_reason": state.get("prediction_engine", {}).get("initial", {}).get("reason"),
            "last_processing_completed_at": last_tick.isoformat(),
            "database": "WAL / serialized writes; responsive snapshot completed",
        }
        state["scalping_monitor"] = diagnose_scalp(
            state, self.scalp_criteria, account=state["account"], now=health_clock
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
            "response_generated_at": utcnow().isoformat(),
            "state_age_seconds": max(0, (utcnow() - last_tick).total_seconds()),
        }
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
            if route == "/":
                body = Path(__file__).with_name("dashboard.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif route in {"/api/state", "/api/details"}:
                value = monitor.snapshot()
                if route == "/api/state" and query.get("view") == ["main"]:
                    # Large raw venue books/trade histories are already journaled;
                    # retrieve them only while Details is open.
                    value["model"] = {
                        k: v
                        for k, v in value.get("model", {}).items()
                        if k
                        not in {"coverage", "scanner", "candidate_features", "market_intelligence"}
                    }
                    value.pop("coverage", None)
                    value.pop("advanced_intelligence", None)
                elif route == "/api/details" and value.get("advanced_intelligence"):
                    value["advanced_intelligence"] = attach_reliability(
                        value["advanced_intelligence"], monitor.engine_report.get()
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
            elif route == "/health":
                body, content_type = b"signals-only dashboard", "text/plain"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Server-Timing",
                "dashboard;dur=" + str(round((perf_counter() - request_started) * 1000, 2)),
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
    try:
        server = ThreadingHTTPServer((args.host, args.port), make_handler(None))
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
    account_candidate = None
    disabled_reason = None
    if args.account_readonly:
        try:
            account_candidate = ReadOnlyKalshi.from_environment()
            disabled_reason = "Checking production authentication and broker read-only scope; account reads remain withheld until verified"
        except AccountReadError as exc:
            disabled_reason = str(exc)
            print(disabled_reason, flush=True)
    feed = PublicFeed() if args.mode == "public" else ReplayFeed(args.replay)
    monitor = Monitor(
        feed,
        Journal(args.journal),
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
        model_registry=args.model_registry
        if args.mode == "public" and not args.disable_model and not args.disable_scheduled_learning
        else None,
    )
    monitor.account_error = disabled_reason
    monitor.account_requested = args.account_readonly
    monitor.account_candidate = account_candidate
    monitor.update_account(utcnow())
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
        while not stop.is_set():
            started = utcnow()
            timer = perf_counter()
            try:
                payload = feed.poll()
                last_success = payload
            except Exception as exc:
                monitor.runtime_meter.error("public_rest", type(exc).__name__)
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
            elapsed = perf_counter() - timer
            mailbox.put((payload, started, utcnow(), elapsed * 1000))
            stop.wait(max(0, args.poll_seconds - elapsed))

    def poll():
        sequence = 0
        previous = None
        source_key = None
        last_processed = -float("inf")
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
                base, started, completed, duration = previous
                if isinstance(base, Exception):
                    monitor.tick(previous)
                else:
                    now = utcnow()
                    enriched = feed.enriched(base, now)
                    monitor.tick((enriched, started, completed, duration))
                last_processed = monotonic()
            else:
                monitor.tick()
                stop.wait(args.poll_seconds)

    server.RequestHandlerClass = make_handler(monitor)
    collector = (
        threading.Thread(target=collect, daemon=True) if isinstance(feed, PublicFeed) else None
    )
    if collector:
        collector.start()
    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    print(
        f"Signals-only {args.mode} dashboard on port {server.server_port}; journal: {args.journal}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
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
        if monitor.scheduled_learning:
            monitor.scheduled_learning.close()
        if isinstance(feed, PublicFeed):
            if feed.streams:
                feed.streams.close()
            if feed.kalshi_stream:
                feed.kalshi_stream.close()
            if feed.hub:
                feed.hub.close()
            if feed.candle_pool:
                feed.candle_pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()

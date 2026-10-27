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
from urllib.parse import urlsplit
import uuid

from .account_readonly import ReadOnlyKalshi, AccountReadError, credential_status
from .account_state import AccountJournal, account_guidance, live_trade_cards
from .scanner import context as scanner_context, qualify as scanner_qualify
from .decision import assess as trade_decision
from .manual import ManualJournal
from .live_model import LiveEstimator
from .learning import load_artifact, predict as learned_predict
from .model import Display, timestamp
from .monitor import Journal, PublicFeed, ReplayFeed, active_market, analyze, utcnow


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
    ):
        self.feed, self.journal = feed, journal
        self.target, self.stop, self.horizon = target, stop, horizon
        self.account_client = account_client
        self.account_candidate = None
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

    def tick(self):
        try:
            payload = self.feed.poll()
            now = timestamp(payload["clock"]) if "clock" in payload else utcnow()
            coverage = payload.setdefault("coverage", {})
            ref = payload.get("reference", {})
            coverage["Coinbase BTC/USD proxy"] = {
                "status": "CONNECTED" if ref.get("price") is not None else "UNAVAILABLE",
                "data": ref,
                "reason": None if ref.get("price") is not None else "Public BTC proxy unavailable",
            }
            market_now = active_market(payload, now)
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
                if self.readiness_at is None or (now - self.readiness_at).total_seconds() >= 60:
                    self.readiness = self.journal.learning_readiness()
                    self.readiness_at = now
                info["learning_status"] = {
                    **self.readiness,
                    "artifact_status": self.artifact_status,
                    "trained_candidate_loaded": self.artifact is not None,
                    "active": "experimental statistical fallback",
                }
                if self.artifact is not None and result.settlement_up is not None:
                    if timestamp(self.artifact["labels_available_through"]) < now:
                        p = learned_predict(
                            self.artifact["model"], info["candidate_features"]["values"]
                        )
                        from dataclasses import replace

                        result = replace(result, settlement_up=p, settlement_down=1 - p)
                        sensitivity = [p]
                        inputs = info.get("settlement_inputs", {})
                        basis = info.get("basis", {})
                        for bound in ("low", "high"):
                            if inputs.get("proxy_price") and basis.get(bound) is not None:
                                f = dict(info["candidate_features"]["values"])
                                f["distance_scaled"] = (
                                    inputs["proxy_price"] + basis[bound] - inputs["strike"]
                                ) / inputs["proxy_price"]
                                sensitivity.append(learned_predict(self.artifact["model"], f))
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
            if info is not None:
                scan = scanner_context(payload, market_now, info, now)
                info["scanner"] = scan
                scanner_qualify(state, scan)
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
            self.update_account(now)
            self.update_manual(state, now)
            self.journal.record_feeds(payload.get("coverage", {}))
            self.journal.record_candles(payload.get("btc_candles", {}), now)
            state["trade_decision"] = trade_decision(state)
            self.journal.step(state)
            if isinstance(self.feed, PublicFeed):
                if (
                    self.settlement_check is None
                    or (now - self.settlement_check).total_seconds() >= 60
                ):
                    self.settlement_check = now
                    with self.journal.connect() as db:
                        tickers = db.execute(
                            """SELECT ticker FROM predictions
                            WHERE mode='PUBLIC MARKET DATA' AND (settlement_up IS NULL OR settlement_origin='legacy_unknown')
                            AND julianday(json_extract(payload,'$.market.closes_at'))<=julianday(?)
                            GROUP BY ticker ORDER BY COALESCE(MAX(outcome_last_attempt),'') ASC,MIN(timestamp) ASC LIMIT 3""",
                            (now.isoformat(),),
                        ).fetchall()
                    with self.journal.connect() as db:
                        for row in tickers:
                            db.execute(
                                "UPDATE predictions SET outcome_last_attempt=? WHERE ticker=?",
                                (now.isoformat(), row[0]),
                            )
                    for row in tickers:
                        if state["market"] and row[0] == state["market"]["ticker"]:
                            continue
                        try:
                            self.journal.settle(
                                row[0], self.feed.resolved(row[0]), "PUBLIC MARKET DATA"
                            )
                        except Exception:
                            state["warnings"].append("Settlement result pending or inaccessible")
            else:
                for ticker, result in payload.get("settlements", {}).items():
                    self.journal.settle(ticker, result, state["mode"])
        except Exception as exc:
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
            self.state = state
            self.last_tick = utcnow()

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
        return self.account_candidate.snapshot()

    def update_account(self, now):
        if self.account_client is None:
            if self.account_candidate is None:
                return
            if self.account_job and self.account_job.done():
                try:
                    batch = self.account_job.result()
                    self.account_journal.bind(self.account_candidate.credential_binding)
                    Path(self.account_journal.path).chmod(0o600)
                    self.account_journal.reconcile(batch)
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
        if self.account_job and self.account_job.done():
            try:
                batch = self.account_job.result()
                self.account_journal.reconcile(batch)
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
            self.account_job = self.account_pool.submit(self.account_client.snapshot, since)

    def account_view(self, state, now):
        if self.account_client is None:
            return {
                "status": "ACCOUNT_CONNECTING"
                if self.account_candidate is not None and self.account_job is not None
                else "PUBLIC_ONLY",
                "reason": self.account_error
                or "No account connection enabled. Public market data only; no positions/orders/fills are read.",
                "holdings": [],
                "guidance": [],
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
            for entry in self.manual.records()["entries"]:
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
        self.manual.guidance(self.manual_quotes, now, state)

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
            market = state.get("market")
            if market:
                elapsed = utcnow() - self.last_tick
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
            if state.get("signal") and utcnow() > timestamp(state["signal"]["expires_at"]):
                state.update(
                    scalp="WAIT",
                    signal=None,
                    scalp_explanation="EXPIRED_SIGNAL: waiting for a fresh qualifying setup",
                )
            state["trade_decision"] = trade_decision(state)
            state["connection_diagnostics"] = {
                "credentials": credential_status(),
                "account_enabled": self.account_client is not None,
                "account_status": state["account"]["status"],
                "account_reason": state["account"].get("reason"),
                "permissions": state["account"].get("permissions") or self.startup_diagnostics,
                "market_stale": state.get("stale", True),
                "market_reason": state.get("explanation"),
                "reference_received_at": state.get("reference", {}).get("observed_at"),
                "quote_received_at": (state.get("market") or {}).get("observed_at"),
                "public_requests": list(getattr(self.feed, "transport_diagnostics", [])),
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
            route = self.path.split("?", 1)[0]
            if route == "/":
                body = Path(__file__).with_name("dashboard.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif route in {"/api/state", "/api/history"}:
                value = monitor.snapshot() if route == "/api/state" else monitor.journal.history()
                if route == "/api/history":
                    value["manual_records"] = monitor.manual.records()
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
                if self.path.startswith("/api/account/"):
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
    parser.add_argument("--poll-seconds", type=float, default=2)
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
        and 0 <= args.manual_delay <= 30
        and 0 <= args.slippage <= 0.10
    ):
        parser.error("target/stop must be in (0,1], horizon 1..900s, polling 1..5s")
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
    )
    monitor.account_error = disabled_reason
    monitor.account_candidate = account_candidate
    monitor.update_account(utcnow())
    stop = threading.Event()
    import signal

    def terminate(_signum, _frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)

    def poll():
        while not stop.is_set():
            monitor.tick()
            stop.wait(args.poll_seconds)

    server.RequestHandlerClass = make_handler(monitor)
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
        monitor.manual_pool.shutdown(wait=False, cancel_futures=True)
        monitor.account_pool.shutdown(wait=False, cancel_futures=True)
        if isinstance(feed, PublicFeed):
            if feed.hub:
                feed.hub.close()
            if feed.candle_pool:
                feed.candle_pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()

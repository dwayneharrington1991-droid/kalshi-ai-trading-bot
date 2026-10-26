"""Read-only browser dashboard. Start manually; defaults to replay, never trading."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from .model import Display, timestamp
from .monitor import Journal, PublicFeed, ReplayFeed, analyze, utcnow


class Monitor:
    def __init__(self, feed, journal, *, target=0.05, stop=0.10, horizon=60):
        self.feed, self.journal = feed, journal
        self.target, self.stop, self.horizon = target, stop, horizon
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
            state = analyze(payload, now, self.target, self.stop, self.horizon)
            self.journal.step(state)
            if isinstance(self.feed, PublicFeed):
                if (
                    self.settlement_check is None
                    or (now - self.settlement_check).total_seconds() >= 60
                ):
                    self.settlement_check = now
                    with self.journal.connect() as db:
                        tickers = db.execute(
                            """SELECT DISTINCT ticker FROM predictions
                            WHERE mode='PUBLIC MARKET DATA' AND settlement_up IS NULL
                            ORDER BY id DESC LIMIT 3"""
                        ).fetchall()
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
            state["connection_error"] = True
            state["checked_at"] = utcnow().isoformat()
        with self.lock:
            self.state = state
            self.last_tick = utcnow()

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
            market = state.get("market")
            if market:
                elapsed = utcnow() - self.last_tick
                clock = timestamp(state["now"]) + elapsed
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
                        estimate=asdict(Display("WAIT", "Stale snapshot or expired contract")),
                    )
            return state


def make_handler(monitor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            route = self.path.split("?", 1)[0]
            if route == "/":
                body = Path(__file__).with_name("dashboard.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            elif route in {"/api/state", "/api/history"}:
                value = monitor.snapshot() if route == "/api/state" else monitor.journal.history()
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
            self.send_error(405, "Read-only dashboard")

        do_PUT = do_PATCH = do_DELETE = do_POST

    return Handler


def main():
    parser = argparse.ArgumentParser(
        description="KXBTC15M read-only public/replay monitor — no orders or credentials"
    )
    parser.add_argument("--mode", choices=["public", "replay"], default="replay")
    parser.add_argument("--replay", type=Path, default=Path(__file__).with_name("demo_replay.json"))
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--poll-seconds", type=float, default=2)
    parser.add_argument("--target", type=float, default=0.05)
    parser.add_argument("--stop", type=float, default=0.10)
    parser.add_argument("--horizon", type=int, default=60)
    args = parser.parse_args()
    if not (
        0 < args.target <= 1
        and 0 < args.stop <= 1
        and 1 <= args.horizon <= 900
        and 1 <= args.poll_seconds <= 5
    ):
        parser.error("target/stop must be in (0,1], horizon 1..900s, polling 1..5s")
    feed = PublicFeed() if args.mode == "public" else ReplayFeed(args.replay)
    monitor = Monitor(
        feed, Journal(args.journal), target=args.target, stop=args.stop, horizon=args.horizon
    )
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            monitor.tick()
            stop.wait(args.poll_seconds)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(monitor))
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


if __name__ == "__main__":
    main()

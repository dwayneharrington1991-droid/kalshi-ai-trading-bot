"""Beginner-friendly PAPER micro-scalp dashboard (separate process and port).

Modes
- ``--live``: polls public Kalshi data (GET-only), optionally reads the
  existing AI dashboard's /api/state GET-only, records to JSONL, and runs the
  paper engine continuously.
- ``--replay FILE``: replays a recording at ``--speed`` x real time.

Safety: binds 127.0.0.1 by default, refuses ports 8765/8767/8768, never sends
anything but GET requests, and has no order endpoint of any kind.

    python -m src.micro_scalp.dashboard --live --port 8791 \
        --record data/micro_scalp/live.jsonl --signals-url http://127.0.0.1:8790/api/state
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from . import install_guard
from .engine import Engine, StrategyConfig
from .events import record_to_event, recorded_events
from .metrics import summarize
from .public_data import PROTECTED_PORTS
from .risk import RiskConfig

HTML = Path(__file__).with_name("dashboard.html")


class EngineHost:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.status = "starting"

    def feed(self, event):
        if event is None:
            return
        with self.lock:
            self.engine.process(event)

    def payload(self):
        with self.lock:
            view = self.engine.view()
            s = summarize(self.engine)
        view["summary"] = {k: s[k] for k in (
            "net_pnl", "return_pct", "completed_trades", "win_rate", "win_rate_95ci", "profit_factor",
            "total_fees", "max_drawdown", "max_drawdown_pct", "orders", "vs_no_trade_baseline")}
        view["feed_status"] = self.status
        return view


def make_handler(host: EngineHost):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/":
                body, ctype = HTML.read_bytes(), "text/html; charset=utf-8"
            elif self.path == "/api/view":
                body, ctype = json.dumps(host.payload(), default=str).encode(), "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self):
            self.send_error(405, "PAPER-ONLY dashboard: read-only, no order endpoints")

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse

    return Handler


def check_port(host: str, port: int):
    if port in PROTECTED_PORTS:
        raise SystemExit(f"Port {port} belongs to an existing deployment; refusing to bind it.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding a non-loopback address; keep this dashboard private.")


def replay_thread(host, path, speed):
    events, _ = recorded_events(path)
    host.status = f"REPLAY x{speed:g} of {path}"
    prev = None
    for e in events:
        if prev is not None and speed > 0:
            time.sleep(max(0.0, min(5.0, (e["at"] - prev) / speed)))
        prev = e["at"]
        host.feed(e)
    host.status = "REPLAY FINISHED"


def live_thread(host, record, signals_url):
    from .recorder import Recorder

    class Feeding(Recorder):
        def write(self, rec):
            super().write(rec)
            try:
                host.feed(record_to_event(rec))
            except (KeyError, TypeError, ValueError):
                pass

    host.status = "LIVE public data (paper only)"
    Feeding(Path(record), signals_url).run(minutes=10**7)


def main():
    install_guard()
    p = argparse.ArgumentParser(description="PAPER micro-scalp dashboard")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    p.add_argument("--speed", type=float, default=20.0)
    p.add_argument("--record", type=Path, default=Path("data/micro_scalp/live.jsonl"))
    p.add_argument("--signals-url", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8791)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--use-ai", action="store_true", help="Add existing AI signals as model inputs")
    p.add_argument("--warm-start", type=Path, help="Historical cache dir to pre-train the model")
    a = p.parse_args()
    check_port(a.host, a.port)
    engine = Engine(StrategyConfig(use_ai=a.use_ai), RiskConfig(starting_balance=a.balance),
                    name="with_existing_ai" if a.use_ai else "market_only")
    if a.warm_start:
        from .events import load_historical

        hist_events, _ = load_historical(a.warm_start)
        warm = Engine(StrategyConfig(), RiskConfig(starting_balance=a.balance)).run(hist_events).model
        engine.model.warm_start_from(warm)
    host = EngineHost(engine)
    target = (lambda: replay_thread(host, a.replay, a.speed)) if a.replay else (lambda: live_thread(host, a.record, a.signals_url))
    threading.Thread(target=target, daemon=True).start()
    server = ThreadingHTTPServer((a.host, a.port), make_handler(host))
    print(f"PAPER micro-scalp dashboard on http://{a.host}:{a.port} (no live orders possible)")
    server.serve_forever()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline market-engine benchmark. No external connections or account access.

Synthetic exchange times are not measured network latency or live throughput.
All journals are temporary. Browser checks, when requested, use only a local
ephemeral HTTP fixture and never the running dashboard on port 8765.
"""
import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
import math
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import threading
from time import perf_counter, process_time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.signals_display.intelligence import MarketIntelligence
from src.signals_display.monitor import Journal, PublicFeed
from src.signals_display.server import Monitor, make_handler
from src.signals_display.streaming import StreamHub
from src.signals_display.advanced import AdvancedIntelligence


def distribution(values):
    if not values:
        return {"samples": 0, "median_ms": None, "p95_ms": None, "maximum_ms": None}
    ordered = sorted(values)
    return {
        "samples": len(values),
        "median_ms": round(statistics.median(values), 4),
        "p95_ms": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 4),
        "maximum_ms": round(max(values), 4),
    }


def price_at(seconds):
    return 100000 + 4 * math.sin(seconds * 0.4) + 0.5 * math.sin(seconds * 1.1)


def history(at, *, received_at=None):
    end = int(at.timestamp()) // 60 * 60
    bars = []
    for i, start in enumerate(range(end - 86400, end, 60)):
        price = 100000 + 5 * math.sin(i * 0.1)
        bars.append(
            dict(
                start=start,
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + math.sin(i),
                volume=1 + i % 20,
            )
        )
    return {
        "source": "Coinbase synthetic benchmark proxy; not live data",
        "received_at": (received_at or at).isoformat(),
        "candles": bars,
    }


def payload(at, opened, *, ticker="KXBTC15M-BENCHMARK", opening_history=False):
    seconds = (at - opened).total_seconds()
    mid = 0.5 + 0.04 * math.sin(seconds * 0.4)
    source = "Coinbase synthetic benchmark proxy; not live data"
    market = {
        "series": "KXBTC15M",
        "status": "open",
        "ticker": ticker,
        "opens_at": opened.isoformat(),
        "closes_at": (opened + timedelta(minutes=15)).isoformat(),
        "observed_at": at.isoformat(),
        "metadata_received_at": at.isoformat(),
        "strike": 100000,
        "settlement_reference": "CF Benchmarks BRTI (synthetic benchmark rules fixture)",
        "mapping_verified": True,
        "averaging_rule_verified": True,
        "comparison_operator": ">=",
        "averaging_seconds": 60,
        "up_definition": "YES follows the verified >= final60s-average benchmark fixture",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "quotes": {
            "up": dict(bid=mid - 0.01, ask=mid + 0.01, bid_size=20, ask_size=20),
            "down": dict(bid=1 - mid - 0.01, ask=1 - mid + 0.01, bid_size=20, ask_size=20),
        },
    }
    candle_data = (
        history(opened - timedelta(minutes=1), received_at=opened - timedelta(milliseconds=100))
        if opening_history
        else history(at)
    )
    return {
        "clock": at.isoformat(),
        "now": at.isoformat(),
        "mode": "REPLAY / SIMULATED",
        "markets": [market],
        "coverage": {},
        "warnings": [],
        "evidence": {},
        "reference": {
            "price": price_at(seconds),
            "source": source,
            "observed_at": at.isoformat(),
            "official": False,
        },
        "btc_candles": candle_data,
    }


def seed_observations(monitor, opened, *, ticker="KXBTC15M-BENCHMARK"):
    for index in range(131):
        at = opened - timedelta(seconds=65 - index * 0.5)
        monitor.estimator.observe(payload(at, opened, ticker=ticker), at)


def tick(monitor, data):
    at = datetime.fromisoformat(data["clock"])
    started = perf_counter()
    monitor.tick((data, at, at, 0.0))
    if monitor.state.get("connection_error"):
        raise RuntimeError(
            "Synthetic prediction fixture failed; inspect implementation before publishing benchmark"
        )
    return (perf_counter() - started) * 1000


def http_samples(opener, base, path, count=10):
    values, size, decoded = [], None, None
    for _ in range(count):
        started = perf_counter()
        with opener.open(base + path, timeout=5) as response:
            raw = response.read()
        values.append((perf_counter() - started) * 1000)
        size = len(raw)
        if path != "/":
            decoded = json.loads(raw)
    return {**distribution(values), "response_bytes": size}, decoded


def browser_samples(base):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "UNAVAILABLE", "reason": "Optional Playwright dependency not installed"}
    results = {}
    with sync_playwright() as playwright:
        chromium = Path("/usr/bin/chromium")
        kwargs = dict(headless=True, args=["--no-sandbox"])
        if chromium.exists():
            kwargs["executable_path"] = str(chromium)
        browser = playwright.chromium.launch(**kwargs)
        try:
            for label, viewport in (
                ("desktop", {"width": 1400, "height": 1100}),
                ("mobile", {"width": 390, "height": 844}),
            ):
                page = browser.new_page(viewport=viewport)
                errors, external = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def only_fixture(route):
                    if route.request.url.startswith(base + "/"):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()

                page.route("**/*", only_fixture)
                started = perf_counter()
                page.goto(base, wait_until="load", timeout=10000)
                load = (perf_counter() - started) * 1000
                page.wait_for_function(
                    "document.getElementById('opening-ticker').textContent.includes('KXBTC15M-')"
                )
                first = (perf_counter() - started) * 1000
                refresh_started = perf_counter()
                page.evaluate("refresh()")
                page.wait_for_function("!stateBusy", timeout=5000)
                refresh = (perf_counter() - refresh_started) * 1000
                results[label] = {
                    "load_ms": round(load, 3),
                    "first_contract_ms": round(first, 3),
                    "refresh_ms": round(refresh, 3),
                    "horizontal_overflow": page.evaluate(
                        "document.documentElement.scrollWidth > window.innerWidth"
                    ),
                    "opening_first": page.evaluate(
                        "document.querySelector('section').id === 'opening-prediction'"
                    ),
                    "opening_direction": page.locator("#opening-direction").inner_text(),
                    "console_errors": errors,
                    "external_requests": len(external),
                }
                page.close()
        finally:
            browser.close()
    return results


def run(args):
    wall_started, cpu_started = perf_counter(), process_time()
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=5)
    stream = StreamHub(autostart=False)
    intelligence = MarketIntelligence()
    advanced = AdvancedIntelligence()
    ingest_ms, analysis_ms, advanced_ms, analysis = [], [], [], {}
    advanced_history, history_minute = None, None
    for index in range(args.events):
        event = base + timedelta(seconds=index * 0.1)
        receipt = event + timedelta(milliseconds=25)
        price = price_at(index * 0.1)
        message = {
            "channel": "ticker",
            "sequence_num": index,
            "timestamp": event.isoformat(),
            "events": [
                {
                    "type": "update",
                    "tickers": [
                        {
                            "product_id": "BTC-USD",
                            "price": str(price),
                            "best_bid": str(price - 0.5),
                            "best_ask": str(price + 0.5),
                        }
                    ],
                }
            ],
        }
        started = perf_counter()
        stream.process_coinbase(message, received_at=receipt, received_monotonic=1000 + index * 0.1)
        for observation in stream.drain_observations():
            intelligence.observe(observation)
            advanced.observe(observation)
        # Explicit synthetic executions exercise real-volume calculations rather
        # than timing only quote-only/unavailable shortcuts.
        advanced.observe(
            dict(
                source="Coinbase BTC/USD stream",
                kind="trade",
                price=price,
                volume=0.25 + (index % 4) * 0.01,
                aggressor="buy" if index % 3 else "sell",
                trade_id="benchmark-" + str(index),
                event_at=event.isoformat(),
                received_at=receipt.isoformat(),
                session_id=observation.get("session_id"),
            )
        )
        ingest_ms.append((perf_counter() - started) * 1000)
        if index % 5 == 0:
            started = perf_counter()
            analysis = intelligence.summarize(receipt, primary_source="Coinbase BTC/USD stream")
            analysis_ms.append((perf_counter() - started) * 1000)
            minute = int(receipt.timestamp()) // 60
            if minute != history_minute:
                advanced_history, history_minute = history(receipt), minute
            started = perf_counter()
            advanced_result = advanced.summarize(
                receipt,
                candles=advanced_history,
                primary_source="Coinbase BTC/USD stream",
                threshold=100000,
            )
            advanced_ms.append((perf_counter() - started) * 1000)
    final_receipt = base + timedelta(seconds=(args.events - 1) * 0.1, milliseconds=25)
    source = analysis.get("sources", {}).get("Coinbase BTC/USD stream", {})
    stream.disconnected(RuntimeError("simulated outage"))
    invalidated = stream.snapshot(final_receipt).get("reference") is None
    stream.begin_session("coinbase")
    reconnect_time = final_receipt + timedelta(seconds=0.1)
    stream.process_coinbase(
        {
            "channel": "ticker",
            "sequence_num": 0,
            "timestamp": reconnect_time.isoformat(),
            "events": [
                {"type": "update", "tickers": [{"product_id": "BTC-USD", "price": "100001"}]}
            ],
        },
        received_at=reconnect_time,
        received_monotonic=1000 + args.events * 0.1,
    )
    recovered = stream.snapshot(reconnect_time).get("reference") is not None
    stream.close()

    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline benchmark attempted external access")

    report = {
        "benchmark": "OFFLINE_SYNTHETIC_ONLY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limitations": [
            "No real network, authenticated account or real exchange feed is measured",
            "Synthetic event-to-receipt offset is configured, not measured network latency",
            "CPU and memory describe this benchmark process, not the user's Codespace",
            "Model forecasts and simulated fills are experimental, never demonstrated accuracy or profitability",
        ],
        "synthetic_input": {
            "events": args.events,
            "event_spacing_ms": 100,
            "configured_receipt_offset_ms": 25,
            "analysis_cadence_simulated_ms": 500,
            "simulated_span_seconds": round((args.events - 1) * 0.1, 3),
        },
        "ingestion_including_buffer_ms": distribution(ingest_ms),
        "intelligence_calculation_ms": distribution(analysis_ms),
        "advanced_trade_and_technical_calculation_ms": distribution(advanced_ms),
        "advanced_fixture": {
            "vwap_status": advanced_result["families"]["vwap"]["status"],
            "flow_status": advanced_result["families"]["order_flow"]["status"],
            "capture_resets": advanced_result["sources"]["Coinbase BTC/USD stream"]["reset_count"],
            "trade_records": advanced_result["sources"]["Coinbase BTC/USD stream"][
                "retained_trade_records"
            ],
            "thirty_second_rsi_status": advanced_result["families"]["rsi"]["reading"]["30s"][
                "status"
            ],
        },
        "retained_buffers": {
            k: source.get(k) for k in ("retained_fine_bins", "retained_minute_bins")
        },
        "simulated_reconnect": {
            "attempts": 1,
            "old_reference_invalidated": invalidated,
            "fresh_post_reconnect_reference_accepted": recovered,
        },
    }
    with tempfile.TemporaryDirectory(prefix="signals-offline-benchmark-") as directory:
        feed = PublicFeed(get=no_network, multi_venue=False)
        monitor = Monitor(
            feed,
            Journal(Path(directory) / "benchmark.db"),
            horizon=args.horizon,
            account_store=Path(directory) / "account.db",
        )
        opened = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        seed_observations(monitor, opened)
        opening_ms = tick(monitor, payload(opened, opened, opening_history=True))
        original = deepcopy(monitor.state.get("prediction_engine", {}).get("initial"))
        for index in range(1, 62):
            at = opened + timedelta(seconds=index * 2)
            monitor.estimator.observe(payload(at, opened), at)
        prediction_ms = []
        for index in range(args.predictions):
            at = opened + timedelta(seconds=124 + index * 0.5)
            prediction_ms.append(tick(monitor, payload(at, opened)))
        report["opening_calculation_with_journal_ms"] = round(opening_ms, 3)
        report["live_prediction_with_journal_ms"] = distribution(prediction_ms)
        report["prediction_fixture"] = {
            "mode": "REPLAY / SIMULATED",
            "scalp_horizon_seconds": args.horizon,
            "opening_status": original.get("status") if original else None,
            "scalp_probabilities_available": monitor.state.get("estimate", {}).get("scalp_up")
            is not None,
        }
        next_open = opened + timedelta(minutes=15)
        seed_observations(monitor, next_open, ticker="KXBTC15M-BENCHMARK-NEXT")
        rollover_ms = tick(
            monitor,
            payload(next_open, next_open, ticker="KXBTC15M-BENCHMARK-NEXT", opening_history=True),
        )
        preserved = monitor.journal.opening_record("KXBTC15M-BENCHMARK", "REPLAY / SIMULATED")
        report["simulated_rollover"] = {
            "processing_ms": round(rollover_ms, 3),
            "current_ticker": monitor.state.get("market", {}).get("ticker"),
            "opening_immutable": preserved == original,
            "latency_scope": "Processing with metadata already supplied; excludes real listing/network delays",
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))  # Local ephemeral fixture only.
        release = threading.Event()
        try:
            report["http_html"], _ = http_samples(opener, url, "/")
            report["http_main_state"], _ = http_samples(opener, url, "/api/state?view=main")
            report["http_history_page"], _ = http_samples(opener, url, "/api/history-page?limit=20")
            history_values = []
            for _ in range(20):
                started = perf_counter()
                monitor.journal.history_page(limit=20)
                history_values.append((perf_counter() - started) * 1000)
            report["sqlite_history_page_ms"] = distribution(history_values)

            def blocked_research():
                if not release.wait(10):
                    raise TimeoutError("Benchmark release missing")
                return {"validated": False, "benchmark": "synthetic blocked research"}

            monitor.research_report.calculate = blocked_research
            http_samples(opener, url, "/api/research", count=1)
            report["http_main_while_research_blocked"], _ = http_samples(
                opener, url, "/api/state?view=main"
            )
            release.set()
            if args.browser:
                report["browser"] = browser_samples(url)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
            monitor.manual_pool.shutdown(wait=True)
            monitor.account_pool.shutdown(wait=True)
            monitor.maintenance_pool.shutdown(wait=True)
            monitor.readiness_pool.shutdown(wait=True)
            monitor.engine_report.close()
            monitor.research_report.close()
            monitor.scalp_paper_report.close()
    elapsed, cpu = perf_counter() - wall_started, process_time() - cpu_started
    report["resources"] = {
        "wall_seconds": round(elapsed, 3),
        "process_cpu_seconds": round(cpu, 3),
        "cpu_percent_one_core": round(100 * cpu / elapsed, 2),
        "peak_resident_mib_linux": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2
        ),
        "note": "Peak RSS belongs to this Python process; browser child processes are not included",
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=3001)
    parser.add_argument("--predictions", type=int, default=20)
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 20 <= args.events <= 100000
        or not 1 <= args.predictions <= 200
        or not 1 <= args.horizon <= 120
    ):
        parser.error("events must be20..100000, predictions1..200, horizon1..120")
    report = run(args)
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()

"""Real Chromium checks against an ephemeral local server and synthetic data only."""
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from src.signals_display.monitor import Journal, analyze
from src.signals_display.server import Monitor, StartupStatus, make_handler, make_startup_handler
from tests.test_signals_monitor import frame
from tests.test_signals_display import NOW


def test_complete_dashboard_remains_accessible_while_intelligence_initializes():
    startup = StartupStatus("startup123")
    startup.update("INTELLIGENCE", "Opening preserved journals")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_startup_handler(startup))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            response = page.goto(f"http://127.0.0.1:{server.server_port}/")
            assert response.status == 200
            playwright.expect(page.locator("#freshness")).to_have_text("WARMING UP")
            playwright.expect(page.locator("#running-build")).to_have_text("startup")
            assert "Opening preserved journals" in page.locator("#market-health-summary").inner_text()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_browser_monitor_panels_rollover_staleness_and_readonly_journal(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "journal.db"), stop=0.15)
    monitor.state = analyze(frame(), NOW, stop=0.15)
    monitor.journal.step(monitor.state)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # Browser must never request anything outside its local mock server.
            unexpected = []

            def route(request):
                if request.request.url.startswith(f"http://127.0.0.1:{server.server_port}/"):
                    request.continue_()
                else:
                    unexpected.append(request.request.url)
                    request.abort()

            page.route("**/*", route)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            assert page.locator("#primary-opening").inner_text() == "NO VERIFIED EDGE"
            assert page.locator("#primary-scalp").inner_text() == "WAIT"
            playwright.expect(page.locator("#direction")).to_have_text("UP")
            assert page.locator("#scalp").text_content() == "WAIT"
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            assert page.locator("#scalp-yesno").inner_text() == "NO"
            assert page.locator("#simple-entry").inner_text() == "Not available"
            assert page.locator("#simple-risk").inner_text() == "Not available"
            assert page.locator("#prob-up").text_content() == "60.0%"
            assert not page.locator("#details").get_attribute("open")
            page.screenshot(path="/tmp/kxbtc15m-simple-main.png", full_page=True)
            page.locator("#details > summary").click()
            playwright.expect(page.locator("#model-scalp")).to_contain_text("DOWN SCALP")
            assert "90% is a goal, not a claim" in page.locator("#accuracy-summary").inner_text()
            assert "$100000.00" in page.locator("#strike").inner_text()
            assert "hypothetical plan" in page.locator("#plans").inner_text()
            assert "I entered — record manually" in page.get_by_role("button").all_text_contents()
            assert not any(
                text in ("Buy", "Sell", "Cancel order")
                for text in page.get_by_role("button").all_text_contents()
            )
            page.screenshot(path="/tmp/kxbtc15m-dashboard-desktop.png", full_page=True)
            data = frame(2)
            data["markets"][0]["quotes"]["up"].update(bid=0.70, ask=0.75)
            data["markets"][0]["quotes"]["down"].update(bid=0.25, ask=0.30)
            monitor.state = analyze(data, NOW + timedelta(seconds=2), stop=0.15)
            monitor.journal.step(monitor.state)
            page.locator("#load-history").click()
            playwright.expect(page.locator("#outcomes")).to_contain_text("SIMULATED_TARGET")
            data = frame(600)
            data["markets"][0].update(
                ticker="KXBTC15M-NEXT",
                opens_at=(NOW + timedelta(seconds=600)).isoformat(),
                closes_at=(NOW + timedelta(seconds=1500)).isoformat(),
            )
            monitor.state = analyze(data, NOW + timedelta(seconds=600))
            playwright.expect(page.locator("#contract")).to_have_text("KXBTC15M-NEXT")
            monitor.state = analyze(data, NOW + timedelta(seconds=606))
            playwright.expect(page.locator("#freshness")).to_contain_text("STALE")
            assert page.locator("#direction").text_content() == "UNCLEAR"
            assert page.locator("#scalp").text_content() == "WAIT"
            assert page.locator("#prob-up").text_content() == "unavailable"
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="/tmp/kxbtc15m-dashboard-mobile.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors and not unexpected
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_browser_shows_separate_live_model_diagnostics_and_estimates(tmp_path):
    from tests.test_signals_live_model import trained, estimate
    from src.signals_display.model import timestamp

    model, payload = trained()
    result, info = estimate(model, payload)
    state = analyze(payload, timestamp(payload["now"]), 0.05, 0.10, 10, result, info)
    # Replay the live-produced state with its virtual clock; no external API.
    state["mode"] = "REPLAY / SIMULATED"
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.locator("#details > summary").click()
            playwright.expect(page.locator("#direction-reason")).to_contain_text("MODEL_ESTIMATE")
            assert page.locator("#prob-up").text_content() == "50.0%"
            assert page.locator("#scalp-up").inner_text() != "unavailable"
            assert page.locator("#scalp-down").inner_text() != "unavailable"
            assert (
                "opening-window anchored proxy/index basis"
                in page.locator("#overall-diagnostics").text_content()
            )
            assert "frozen current spread" in page.locator("#scalp-diagnostics").text_content()
            monitor.state = {
                **state,
                "explanation": "INSUFFICIENT_HISTORY: reference 1/61 samples, 0/120s span",
                "scalp_explanation": "UNKNOWN_FEES: no supported series fee schedule",
            }
            page.evaluate("refreshDetails()")
            playwright.expect(page.locator("#direction-reason")).to_contain_text(
                "INSUFFICIENT_HISTORY"
            )
            playwright.expect(page.locator("#scalp-reason")).to_contain_text("UNKNOWN_FEES")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_browser_actual_account_cards_pending_fills_and_local_selection(tmp_path):
    from src.signals_display.monitor import utcnow
    from tests.test_signals_account_readonly import snap

    now = utcnow()
    monitor = Monitor(
        None,
        Journal(tmp_path / "journal.db"),
        account_client=object(),
        account_store=tmp_path / "account.db",
    )
    monitor.state = analyze(frame(), NOW)
    data = snap()
    data["observed_at"] = now.isoformat()
    data["orders"] = [
        dict(order_id="order1", ticker="KXBTC15M-MOCK", status="resting", remaining_count_fp="8")
    ]
    monitor.account_journal.reconcile(data)
    monitor.account_connected = True
    quote = frame()["markets"][0]
    quote.update(observed_at=now.isoformat(), closes_at=(now + timedelta(seconds=600)).isoformat())
    monitor.manual_quotes["KXBTC15M-MOCK"] = quote
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#account-status")).to_contain_text(
                "READ-ONLY CONNECTED"
            )
            playwright.expect(page.locator("#live-trade-cards")).to_contain_text("$0.82")
            assert page.locator("#live-trade-cards .manual-record").count() == 1
            assert page.locator("#account-balance").inner_text() == "$1000.00"
            assert "DATA UNAVAILABLE" in page.locator("#live-trade-cards").inner_text()
            page.locator("#account-pending > summary").click()
            playwright.expect(page.locator("#account-order-rows")).to_contain_text("8")
            page.locator("#live-trade-cards button").click()
            playwright.expect(page.locator("#live-trade-cards button")).to_contain_text(
                "Stop guidance"
            )
            assert monitor.account_journal.snapshot()["holdings"][0]["selected"] == 1
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_history_failure_does_not_mark_public_or_account_state_disconnected(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = analyze(frame(), NOW)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            page.route(
                "**/api/history-page?*",
                lambda route: route.fulfill(status=503, body="{}", content_type="application/json"),
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#contract")).to_have_text("KXBTC15M-MOCK")
            page.locator("#details > summary").click()
            page.locator("#load-history").click()
            playwright.expect(page.locator("#journal-notice")).to_contain_text(
                "History refresh delayed"
            )
            playwright.expect(page.locator("#account-status")).to_have_text(
                "PUBLIC MODE · ACCOUNT OFF"
            )
            assert page.locator("#freshness").text_content() == "LIVE"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_blocked_permission_is_visible_instead_of_generic_account_off(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = analyze(frame(), NOW)
    monitor.account_candidate = object()
    monitor.account_error = (
        "READ_ONLY_SCOPE_NOT_PROVEN: authenticated metadata has insufficient scopes"
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#account-status")).to_have_text(
                "ACCOUNT BLOCKED — SEE REASON"
            )
            assert "READ_ONLY_SCOPE_NOT_PROVEN" in page.locator("#account-sync").inner_text()
            assert page.locator("#account-balance").inner_text() == "unavailable"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_prediction_boxes_preserve_opening_when_live_probability_changes(tmp_path):
    from tests.test_signals_prediction_engine import history, market, points
    from src.signals_display import prediction_engine as engine

    data = frame()
    data["markets"] = [market()]
    state = analyze(data, NOW)
    state["model"] = {"analysis_24h": engine.context24(history(), NOW)}
    state["estimate"]["settlement_up"] = 0.6
    opening = engine.opening_forecast(
        market(), history(NOW - timedelta(minutes=1), NOW - timedelta(seconds=1)), points(), NOW
    )
    state["prediction_engine"] = engine.view(state, None, opening, NOW)
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#engine-up")).to_have_text("60.0%")
            assert page.locator("#engine-down").inner_text() == "40.0%"
            assert page.locator("#opening-direction").inner_text() in ("UP", "DOWN")
            assert page.locator("#opening-ticker").inner_text() == market()["ticker"]
            assert page.locator("#opening-generated").inner_text() == opening["generated_at"]
            assert page.locator("#opening-cutoff").inner_text() == NOW.isoformat()
            assert page.locator("#opening-prediction").evaluate(
                "e=>e.getBoundingClientRect().top"
            ) < page.locator("#btc-engine").evaluate("e=>e.getBoundingClientRect().top")
            assert page.locator("#btc-engine").evaluate(
                "e=>e.getBoundingClientRect().top"
            ) < page.locator("#next-move").evaluate("e=>e.getBoundingClientRect().top")
            initial = page.locator("#engine-up-initial").inner_text()
            frozen_label = page.locator("#opening-direction").inner_text()
            frozen_probability = page.locator("#opening-up-prob").inner_text()
            monitor.state = {**state, "estimate": {**state["estimate"], "settlement_up": 0.4}}
            monitor.state["prediction_engine"] = engine.view(
                monitor.state, None, opening, NOW + timedelta(seconds=1)
            )
            playwright.expect(page.locator("#engine-up")).to_have_text("40.0%")
            assert page.locator("#engine-up-initial").inner_text() == initial
            assert page.locator("#opening-direction").inner_text() == frozen_label
            assert page.locator("#opening-up-prob").inner_text() == frozen_probability
            assert page.locator("#live-direction").inner_text() == "DOWN"
            page.set_viewport_size({"width": 390, "height": 844})
            assert (
                page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
                and not errors
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_main_forecasts_stay_responsive_while_requested_history_is_blocked(tmp_path):
    """A slow read must not block new contract state or overwrite it later."""
    from time import perf_counter

    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = analyze(frame(), NOW)
    monitor.state["runtime"] = {"state_version": 2, "published_at": NOW.isoformat()}
    history_entered, release_history = threading.Event(), threading.Event()
    original_page = monitor.journal.history_page

    def slow_page(**kwargs):
        history_entered.set()
        release_history.wait(timeout=10)
        return original_page(**kwargs)

    monitor.journal.history_page = slow_page
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            requests, errors = [], []
            page.on("request", lambda request: requests.append(request.url))
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#opening-ticker")).to_have_text("KXBTC15M-MOCK")
            assert (
                page.locator("#opening-direction").inner_text() == "OPENING PREDICTION UNAVAILABLE"
            )
            assert not any(
                "/api/history" in url or "view=details" in url or "/api/research" in url
                for url in requests
            )
            page.locator("#details > summary").click()
            page.locator("#load-history").click()
            assert history_entered.wait(timeout=2)
            newer = analyze(frame(1), NOW + timedelta(seconds=1))
            newer["market"]["ticker"] = "KXBTC15M-ROLLED"
            newer["runtime"] = {
                "state_version": 3,
                "published_at": (NOW + timedelta(seconds=1)).isoformat(),
            }
            monitor.state = newer
            started = perf_counter()
            page.evaluate("refresh()")
            playwright.expect(page.locator("#opening-ticker")).to_have_text("KXBTC15M-ROLLED")
            assert perf_counter() - started < 2
            assert not release_history.is_set()
            assert (
                page.locator("#opening-direction").inner_text() == "OPENING PREDICTION UNAVAILABLE"
            )
            # A response bearing an older published version must not restore the old ticker.
            monitor.state = {
                **newer,
                "market": {**newer["market"], "ticker": "KXBTC15M-OLD"},
                "runtime": {"state_version": 2, "published_at": NOW.isoformat()},
            }
            page.evaluate("refresh()")
            assert page.locator("#opening-ticker").inner_text() == "KXBTC15M-ROLLED"
            release_history.set()
            playwright.expect(page.locator("#history-page-status")).to_contain_text("Page 1")
            assert page.locator("#opening-ticker").inner_text() == "KXBTC15M-ROLLED"
            assert not errors
            browser.close()
    finally:
        release_history.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_model_market_chart_alerts_and_costs_stay_experimental_and_readonly(tmp_path):
    """Positive theoretical value never becomes a trade instruction; chart rejects future rows."""
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    state = analyze(frame(), NOW)
    ticker = state["market"]["ticker"]
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL",
        "ticker": ticker,
        "threshold": 100000,
        "up": 0.65,
        "down": 0.35,
        "updated_at": NOW.isoformat(),
        "data_cutoff": NOW.isoformat(),
        "reference": state["reference"],
        "calibration": "UNCALIBRATED — experimental; edge unverified",
    }
    state["market_comparison"] = {
        "market_implied_up": 0.5,
        "market_implied_down": 0.5,
        "model_minus_market": 0.15,
    }
    # A missing executable ask size must stay unavailable after server-side
    # comparison is recalculated; a hand-supplied derived panel is not authoritative.
    state["market"]["quotes"]["down"]["ask_size"] = 0
    state["market"]["quotes"]["up"]["ask_size"] = 12
    state["model_market"] = {
        "status": "EDGE UNVERIFIED",
        "validated": False,
        "reason": "Experimental cost-adjusted comparison. Profitable edge unverified.",
        "uncertainty": "Unmeasured proxy/index basis and calibration error.",
        "sides": {
            "up": {
                "model_probability": 0.65,
                "bid": 0.48,
                "ask": 0.52,
                "spread": 0.04,
                "ask_size": 12,
                "entry_fee": 0.02,
                "slippage": 0.01,
                "net_ev": 0.10,
                "sensitivity_net_ev": [-0.01, 0.15],
                "executable": True,
            },
            "down": {"model_probability": 0.35, "executable": False},
        },
    }
    state["monitor_alerts"] = [
        {
            "id": "risk-1",
            "type": "RISK_LIMIT_BREACH",
            "title": "Configured risk limit",
            "reason": "Check manually <img src=x onerror=alert(1)>",
            "timestamp": NOW.isoformat(),
        }
    ]
    monitor.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/api/probability-history?*",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=__import__("json").dumps(
                        {
                            "ticker": ticker,
                            "points": [
                                {"timestamp": (NOW - timedelta(seconds=2)).isoformat(), "up": 0.6},
                                {"timestamp": NOW.isoformat(), "up": 0.65},
                                {
                                    "timestamp": (NOW + timedelta(seconds=20)).isoformat(),
                                    "up": 0.99,
                                },
                            ],
                        }
                    ),
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#compare-up-ev")).to_have_text("$0.10 (experimental)")
            assert page.locator("#model-market-status").inner_text() == "EDGE UNVERIFIED"
            assert page.locator("#compare-down-ev").inner_text() == "unavailable"
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            assert page.locator("#scalp-yesno").inner_text() == "NO"
            assert "+$20.00" in page.locator("#live-distance").inner_text()
            playwright.expect(page.locator("#probability-history-status")).to_contain_text(
                "2 recorded model points"
            )
            assert page.evaluate("probabilityPoints.every(point=>point.up<.99)")
            order = [
                page.locator(selector).evaluate("element=>element.getBoundingClientRect().top")
                for selector in (
                    "#opening-prediction",
                    "#btc-engine",
                    "#model-market-panel",
                    "#live-trades-panel",
                )
            ]
            assert order == sorted(order)
            playwright.expect(page.locator("#monitor-alert-events")).to_contain_text(
                "Configured risk limit"
            )
            assert page.locator("#monitor-alert-events img").count() == 0
            for _ in range(3):
                page.evaluate("monitorAlerts(lastState)")
            assert page.locator("#monitor-alert-events li").count() == 1
            page.locator("#alert-preferences > summary").click()
            page.locator("#alert-probability-threshold").fill("12")
            page.locator("#alert-settings-form button").click()
            assert (
                page.evaluate(
                    "JSON.parse(localStorage.getItem('kxbtc-alert-preferences')).probabilityThreshold"
                )
                == 12
            )
            page.locator("#model-market-details > summary").click()
            assert "12" in page.locator("#model-market-costs").inner_text()
            assert "$0.02" in page.locator("#model-market-costs").inner_text()
            page.evaluate("expired('Synthetic connection outage')")
            assert page.locator("#compare-up-ev").inner_text() == "unavailable"
            assert page.locator("#compare-up-ask").inner_text() == "unavailable"
            assert page.locator("#model-market-status").inner_text() == "EDGE UNVERIFIED"
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_probability_history_cannot_restore_a_previous_contract_after_rollover(tmp_path):
    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    monitor.state = analyze(frame(), NOW)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            pending = []
            page.route("**/api/probability-history?*", lambda route: pending.append(route))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#opening-ticker")).to_have_text("KXBTC15M-MOCK")
            assert len(pending) == 1
            newer = analyze(frame(1), NOW + timedelta(seconds=1))
            newer["market"]["ticker"] = "KXBTC15M-NEXT"
            monitor.state = newer
            page.evaluate("refresh()")
            playwright.expect(page.locator("#opening-ticker")).to_have_text("KXBTC15M-NEXT")
            assert page.evaluate("probabilityTicker") == "KXBTC15M-NEXT"
            assert page.evaluate("probabilityPoints.length") == 0
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_neutral_opening_and_scalp_monitor_explain_fallback_without_trade_promotion(tmp_path):
    """Rounded ties stay neutral; hypothetical criteria cannot promote an entry."""
    from copy import deepcopy
    import json

    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    state = analyze(frame(), NOW)
    ticker = state["market"]["ticker"]
    initial = {
        "ticker": ticker,
        "up": 0.49994,
        "direction": "DOWN",  # Historical stored label remains immutable.
        "model": "opening24h-zero-drift-final-average-v1",
        "generated_at": NOW.isoformat(),
        "data_cutoff": NOW.isoformat(),
        "opening_delay_seconds": 2,
        "status": "DELAYED",
        "reason": "Frozen inputs; experimental zero-drift baseline.",
        "threshold": 100000,
    }
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL",
        "ticker": ticker,
        "threshold": 100000,
        "up": 0.49994,
        "down": 0.50006,
        "initial": deepcopy(initial),
        "updated_at": NOW.isoformat(),
        "reference": state["reference"],
        "opening_diagnostics": {
            "model_id": initial["model"],
            "fallback_reason": "80 official outcomes; opening-specific validation minimum not met.",
            "raw_probability_up": 0.49994,
            "live_ensemble_active": False,
            "opening_ensemble_active": False,
        },
    }
    criteria = {
        "quantity": 1.0,
        "minimum_success_probability": 0.6,
        "maximum_spread": 0.03,
        "minimum_entered_paths": 200,
        "minimum_net_ev": 0.0,
        "maximum_data_age_seconds": 5.0,
        "maximum_source_skew_seconds": 5.0,
        "maximum_holding_seconds": 60,
    }
    state["scalping_monitor"] = {
        "engine_status": "ACTIVE",
        "data_status": "FRESH",
        "model_status": "EXPERIMENTAL",
        "liquidity_status": "SUFFICIENT",
        "signal_status": "CANDIDATE",
        "recommendation": "WAIT",
        "actionable": False,
        "exact_reason": "SCALP_PROMOTION_BLOCKED: no reviewed evidence after costs <img src=x onerror=alert(1)>",
        "ticker": ticker,
        "timestamp": NOW.isoformat(),
        "model_version": "experimental-logit-diffusion",
        "remaining_seconds": 600,
        "criteria": criteria,
        "data_ages": {"reference_seconds": 0.2, "quote_seconds": 0.1},
        "exposure": {"confirmed_quantity": 2},
        "sides": {
            "up": {
                "bid": 0.49,
                "ask": 0.51,
                "bid_size": 20,
                "ask_size": 20,
                "spread": 0.02,
                "entry_fee": 0.02,
                "slippage": 0.01,
                "entry_cap": 0.52,
                "target_bid": 0.61,
                "stop_bid": 0.40,
                "holding_seconds": 60,
                "target_probability": 0.65,
                "net_expected_profit": 0.02,
                "hypothetical_candidate": True,
                "reasons": ["Experimental Monte Carlo paths are not validated execution evidence."],
            }
        },
    }
    state["runtime"] = {"published_at": NOW.isoformat(), "state_version": 1}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1100})
            errors, writes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/api/state?*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(state)
                ),
            )

            def criteria_route(route):
                if route.request.method == "POST":
                    writes.append((route.request.url, route.request.post_data_json))
                    criteria.update(route.request.post_data_json)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"criteria": criteria}),
                )

            paper_requests = []

            def paper_route(route):
                paper_requests.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "READY",
                            "generated_at": NOW.isoformat(),
                            "report": {
                                "status": "PAPER_REPLAY_ONLY",
                                "coverage": {"accepted_book_records": 15},
                                "strategies": {
                                    "candidate": {
                                        "opportunities": 2,
                                        "simulated_trades": 1,
                                        "win_rate_observed_only": 1.0,
                                        "net_simulated_pnl": 0.15,
                                        "maximum_drawdown": 0.0,
                                        "average_net_return_per_observed_trade": 0.15,
                                        "trades": [{"audit": "DO_NOT_RENDER_TRADE_ARRAY"}] * 2000,
                                    }
                                },
                                "profitable_edge_demonstrated": False,
                            },
                        }
                    ),
                )

            page.route("**/api/scalp-criteria", criteria_route)
            page.route("**/api/scalp-paper-performance", paper_route)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#opening-direction")).to_have_text("INSUFFICIENT PREDICTIVE CONFIDENCE")
            assert page.locator("#opening-up-prob").inner_text() == "50.0%"
            assert page.locator("#opening-down-prob").inner_text() == "50.0%"
            assert page.locator("#live-direction").inner_text() == "NEUTRAL"
            assert (
                paper_requests == []
            )  # Paper history is explicitly requested, never polled by the main view.
            assert page.evaluate("directionLabel(0.5000000000000507)") == "NEUTRAL"
            assert "80 official outcomes" in page.locator("#opening-model-summary").inner_text()
            assert page.evaluate("lastState.prediction_engine.initial.direction") == "DOWN"
            assert page.evaluate("lastState.prediction_engine.initial.up") == 0.49994
            order = [
                page.locator(selector).evaluate("e=>e.getBoundingClientRect().top")
                for selector in (
                    "#opening-prediction",
                    "#btc-engine",
                    "#scalping-monitor",
                    "#model-market-panel",
                    "#live-trades-panel",
                )
            ]
            assert order == sorted(order)
            assert page.locator("#scalp-monitor-signal").inner_text() == "CANDIDATE"
            assert page.locator("#scalp-monitor-action").inner_text() == "WAIT"
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            assert page.locator("#scalp-monitor-model").inner_text() == "EXPERIMENTAL"
            assert "SCALP_PROMOTION_BLOCKED" in page.locator("#scalp-monitor-reason").inner_text()
            assert page.locator("#scalp-monitor-reason img").count() == 0
            page.locator("#scalp-hypotheses > summary").click()
            playwright.expect(page.locator("#scalp-hypothesis-plans")).to_contain_text("$0.52")
            assert "hypothetical only" in page.locator("#scalp-hypothesis-plans").inner_text()
            page.locator("#details > summary").click()
            playwright.expect(page.locator("#opening-model-diagnostics")).to_contain_text(
                "raw_probability_up"
            )
            assert paper_requests == []
            page.locator("#load-scalp-paper").click()
            playwright.expect(page.locator("#scalp-paper-status")).to_contain_text(
                "EDGE UNVERIFIED"
            )
            assert len(paper_requests) == 1
            assert "$0.15" in page.locator("#scalp-paper-rows").inner_text()
            assert (
                "DO_NOT_RENDER_TRADE_ARRAY" not in page.locator("#scalp-paper-details").inner_text()
            )
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            page.locator("#scalp-criteria-details > summary").click()
            playwright.expect(page.locator("#save-scalp-criteria")).to_be_enabled()
            page.locator("input[name=minimum_success_probability]").fill("0.7")
            page.locator("#save-scalp-criteria").click()
            playwright.expect(page.locator("#scalp-criteria-status")).to_contain_text(
                "Diagnostic criteria saved"
            )
            assert len(writes) == 1 and writes[0][1]["minimum_success_probability"] == 0.7
            assert writes[0][0].endswith("/api/scalp-criteria")
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            state["prediction_engine"].update(up=0.7, down=0.3)
            state["runtime"] = {
                "published_at": (NOW + timedelta(seconds=1)).isoformat(),
                "state_version": 2,
            }
            page.evaluate("refresh()")
            playwright.expect(page.locator("#live-direction")).to_have_text("UP")
            assert page.locator("#opening-direction").inner_text() == "INSUFFICIENT PREDICTIVE CONFIDENCE"
            assert state["prediction_engine"]["initial"] == initial
            page.evaluate("expired('Synthetic outage')")
            assert page.locator("#scalp-monitor-data").inner_text() == "STALE"
            assert page.locator("#scalp-monitor-signal").inner_text() == "BLOCKED"
            assert "$0.49" not in page.locator("#scalp-monitor-quotes").inner_text()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_advanced_intelligence_is_lazy_safe_and_integrated_with_forecast_panels(tmp_path):
    """All twelve diagnostics remain hidden until requested and cannot replace a forecast."""
    import json

    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    state = analyze(frame(), NOW)
    ticker = state["market"]["ticker"]
    opening = {
        "ticker": ticker,
        "up": 0.65,
        "down": 0.35,
        "direction": "UP",
        "threshold": 100000,
        "generated_at": NOW.isoformat(),
        "data_cutoff": NOW.isoformat(),
        "reason": "Synthetic frozen opening for this offline browser test.",
    }
    state["prediction_engine"] = {
        "status": "EXPERIMENTAL",
        "ticker": ticker,
        "threshold": 100000,
        "up": 0.4,
        "down": 0.6,
        "initial": opening,
        "updated_at": NOW.isoformat(),
        "reference": state["reference"],
    }
    names = (
        "rsi", "macd", "vwap", "bollinger", "ema", "volume_profile",
        "order_flow", "order_book", "cvd", "microstructure", "ensemble", "calibration",
    )
    state["advanced_intelligence"] = {
        "status": "SHADOW",
        "generated_at": NOW.isoformat(),
        "strongest_validated_factors": [],
        "families": {
            name: {
                "status": "EXPERIMENTAL" if index < 10 else "UNAVAILABLE",
                "reading": {"1m": 52.125, "5m": "warming up"},
                "interpretation": "NEUTRAL",
                "as_of": NOW.isoformat(),
                "age_seconds": 0.25,
                "contributing": False,
                "reason": "Shadow analysis only <img src=x onerror=alert(1)>",
            }
            for index, name in enumerate(names)
        },
    }
    state["advanced_intelligence"]["families"]["vwap"].update(
        reading=None, reason="Genuine executed trade volume unavailable; no substitute used."
    )
    monitor.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1100})
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def state_route(route):
                requests.append(route.request.url)
                route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(state)
                )

            page.route("**/api/state?*", state_route)
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            playwright.expect(page.locator("#opening-direction")).to_have_text("UP")
            assert page.locator("#live-direction").inner_text() == "DOWN"
            assert page.locator("#advanced-rows tr").count() == 0
            assert page.locator("#reliability-chart").get_attribute("data-opening-points") is None
            assert "Open this section" in page.locator("#advanced-full-json").text_content()
            assert not any("view=details" in url for url in requests)
            page.locator("#details > summary").click()
            playwright.expect(page.locator("#opening-model-diagnostics")).to_have_text("{}")
            assert page.locator("#advanced-rows tr").count() == 0
            page.locator("#advanced-market-intelligence > summary").click()
            playwright.expect(page.locator("#advanced-rows tr")).to_have_count(12)
            assert "No advanced factors have been validated" in page.locator("#advanced-factors").inner_text()
            assert "0.25s old" in page.locator("#advanced-rows").inner_text()
            assert "No — shadow / diagnostic" in page.locator("#advanced-rows").inner_text()
            assert "Genuine executed trade volume unavailable" in page.locator("#advanced-rows").inner_text()
            assert page.locator("#advanced-rows img").count() == 0
            assert page.locator("#reliability-chart").get_attribute("data-opening-points") == "0"
            assert "no nonempty, valid official-outcome" in page.locator("#reliability-chart-status").inner_text()
            assert "Open this section" in page.locator("#advanced-full-json").text_content()
            state["advanced_intelligence"]["families"]["vwap"]["session_vwap"] = 100123.456
            state["advanced_intelligence"]["families"]["vwap"]["trades"] = [{"DO_NOT_RENDER_RAW_TRADE": 1}] * 1000
            page.evaluate("advancedState.advanced_intelligence.families.vwap.session_vwap=100123.456; advancedState.advanced_intelligence.families.vwap.trades=[{DO_NOT_RENDER_RAW_TRADE:1}]")
            page.locator("#advanced-full-readings > summary").click()
            playwright.expect(page.locator("#advanced-full-json")).to_contain_text("100123.456")
            assert "DO_NOT_RENDER_RAW_TRADE" not in page.locator("#advanced-full-json").inner_text()
            assert page.locator("#advanced-full-json img").count() == 0
            assert page.locator("#opening-direction").inner_text() == "UP"
            assert page.locator("#opening-up-prob").inner_text() == "65.0%"
            assert page.locator("#live-direction").inner_text() == "DOWN"
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            # An unchanged full snapshot does not recreate twelve rows every refresh.
            page.locator("#advanced-rows tr").first.evaluate("e => e.dataset.retained = 'yes'")
            page.evaluate("advancedView(advancedState)")
            assert page.locator("#advanced-rows tr").first.get_attribute("data-retained") == "yes"
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_reliability_diagram_uses_observed_bins_and_clears_unavailable_results(tmp_path):
    """The chart plots supplied official bins only, never empty or invalid bins."""
    import json

    monitor = Monitor(None, Journal(tmp_path / "journal.db"))
    state = analyze(frame(), NOW)
    state["advanced_intelligence"] = {
        "status": "EXPERIMENTAL_SHADOW",
        "generated_at": NOW.isoformat(),
        "families": {
            "calibration": {
                "status": "EXPERIMENTAL",
                "interpretation": "UNCALIBRATED",
                "contributing": False,
                "reading": {
                    "opening": {
                        "samples": 20,
                        "brier": 0.16,
                        "log_loss": 0.5,
                        "expected_calibration_error": 0.1,
                        "calibration": [
                            {"samples": 10, "mean_probability": 0.2, "outcome_rate": 0.1,
                             "outcome_rate_interval": [0.02, 0.4]},
                            {"samples": 10, "mean_probability": 0.8, "outcome_rate": 0.9,
                             "outcome_rate_interval": [0.6, 0.98]},
                            {"samples": 0, "mean_probability": 0.55, "outcome_rate": 0.2},
                            {"samples": 5, "mean_probability": None, "outcome_rate": 0.5},
                        ],
                    },
                    "live": {
                        "samples": 10,
                        "brier": 0.24,
                        "log_loss": 0.7,
                        "expected_calibration_error": 0.1,
                        "calibration": [
                            {"samples": 10, "mean_probability": 0.4, "outcome_rate": 0.5,
                             "outcome_rate_interval": [0.23, 0.77]},
                            {"samples": 10, "mean_probability": 1.1, "outcome_rate": 0.5},
                        ],
                    },
                },
            }
        },
    }
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(monitor))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1400, "height": 1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route(
                "**/api/state?*",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(state)
                ),
            )
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            page.locator("#details > summary").click()
            assert page.locator("#reliability-chart").get_attribute("data-opening-points") is None
            page.locator("#advanced-market-intelligence > summary").click()
            playwright.expect(page.locator("#reliability-chart-status")).to_contain_text("20 scored outcomes")
            assert page.locator("#reliability-chart").get_attribute("data-opening-points") == "2"
            assert page.locator("#reliability-chart").get_attribute("data-live-points") == "1"
            assert "Brier 0.1600" in page.locator("#reliability-chart-status").inner_text()
            assert "Edge unverified" in page.locator("#reliability-chart").get_attribute("aria-label")
            before = page.locator("#reliability-chart").evaluate("e => e.toDataURL()")
            page.evaluate("advancedView(advancedState)")
            assert page.locator("#reliability-chart").evaluate("e => e.toDataURL()") == before
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.evaluate("advancedState.advanced_intelligence.families.calibration.reading={opening:{samples:0,calibration:[]},live:{samples:0,calibration:[]}}; advancedView(advancedState)")
            playwright.expect(page.locator("#reliability-chart-status")).to_contain_text("unavailable")
            assert page.locator("#reliability-chart").get_attribute("data-opening-points") == "0"
            assert page.locator("#reliability-chart").get_attribute("data-live-points") == "0"
            assert page.locator("#reliability-chart").evaluate("e => e.toDataURL()") != before
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

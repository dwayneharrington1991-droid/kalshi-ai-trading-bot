"""Real Chromium checks against an ephemeral local server and synthetic data only."""
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from src.signals_display.monitor import Journal, analyze
from src.signals_display.server import Monitor, make_handler
from tests.test_signals_monitor import frame
from tests.test_signals_display import NOW


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
            assert page.locator("#opening-prediction").evaluate("e=>e.getBoundingClientRect().top") < page.locator("#btc-engine").evaluate("e=>e.getBoundingClientRect().top")
            assert page.locator("#btc-engine").evaluate("e=>e.getBoundingClientRect().top") < page.locator("#next-move").evaluate("e=>e.getBoundingClientRect().top")
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
            assert page.locator("#opening-direction").inner_text() == "OPENING PREDICTION UNAVAILABLE"
            assert not any("history" in url or "view=details" in url or "/api/research" in url for url in requests)
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
            assert page.locator("#opening-direction").inner_text() == "OPENING PREDICTION UNAVAILABLE"
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

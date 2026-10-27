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
            assert "DOWN SCALP" in page.locator("#model-scalp").text_content()
            assert page.locator("#trade-action").inner_text() == "🟡 WAIT"
            assert page.locator("#scalp-yesno").inner_text() == "NO"
            assert page.locator("#simple-entry").inner_text() == "Not available"
            assert page.locator("#simple-risk").inner_text() == "Not available"
            assert page.locator("#prob-up").text_content() == "60.0%"
            assert not page.locator("#details").get_attribute("open")
            page.screenshot(path="/tmp/kxbtc15m-simple-main.png", full_page=True)
            page.locator("#details > summary").click()
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
            assert "8" in page.locator("#account-order-rows").inner_text()
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

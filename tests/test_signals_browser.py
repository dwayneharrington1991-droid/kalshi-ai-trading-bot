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
            assert page.locator("#scalp").inner_text() == "DOWN SCALP"
            assert page.locator("#prob-up").inner_text() == "60.0%"
            assert "90% is a goal, not a claim" in page.locator("#accuracy-summary").inner_text()
            assert "$100000.00" in page.locator("#strike").inner_text()
            assert "hypothetical plan" in page.locator("#plans").inner_text()
            assert page.get_by_role("button").all_text_contents() == [
                "Download research journal JSON"
            ]
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
            assert page.locator("#direction").inner_text() == "UNCLEAR"
            assert page.locator("#scalp").inner_text() == "WAIT"
            assert page.locator("#prob-up").inner_text() == "unavailable"
            page.set_viewport_size({"width": 390, "height": 844})
            page.screenshot(path="/tmp/kxbtc15m-dashboard-mobile.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors and not unexpected
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

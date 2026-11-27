"""Two-field AI terminal: page contract and real-browser behaviour.

The terminal shows only AI OPENING CALL and AI CONFIDENCE. Every other panel
stays in the page (hidden) so background systems keep running.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src" / "signals_display" / "dashboard.html"


def page():
    return PAGE.read_text(encoding="utf-8")


def test_only_the_two_terminal_fields_are_visible_and_backend_panels_remain():
    html = page()
    assert "main>:not(#minimal-opening){display:none!important}" in html
    assert "<h1>AI OPENING CALL</h1>" in html and "<h1>AI CONFIDENCE</h1>" in html
    for preserved in ("ai-chart-canvas", "scalping-monitor", "live-trades-panel",
                      "primary-live", "primary-scalp", "opening-score"):
        assert f'id="{preserved}"' in html, preserved


def test_terminal_theme_accessibility_and_motion_controls():
    html = page()
    for token in ("--t-cyan", "--t-violet", "--t-up", "--t-down", "--t-wait", "backdrop-filter"):
        assert token in html
    assert "@media(prefers-reduced-motion:reduce)" in html
    assert "@media(max-width:760px){#minimal-opening{grid-template-columns:1fr" in html
    assert 'aria-live="polite">WAIT</strong>' in html


def test_terminal_never_uses_live_estimate_or_uncalibrated_confidence():
    html = page()
    start = html.index("function terminalView(")
    body = html[start: html.index("function primaryView(")]
    # Fed only by the frozen opening record `i`; never the live engine estimate.
    assert "e.up" not in body and "e.direction" not in body
    assert "['CALIBRATED','EXPERIMENTAL CALIBRATED CANDIDATE'].includes(i.confidence?.status)" in body
    assert "uncalibrated estimate withheld" in body
    assert "Date.now()>=closes" in body
    assert "function expired(reason){terminalUnavailable(" in html


NODE_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const page = await b.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/**', r => r.abort());
  await page.goto(process.argv[2]);
  const now = Date.now(), open = new Date(now - 60000).toISOString(), close = new Date(now + 840000).toISOString();
  const run = (i, usable, dir) => page.evaluate(([i, usable, dir]) => {
    terminalView({market: {ticker: 'KXBTC15M-T'}}, i, usable, dir, Math.max(i.up, 1 - i.up));
    const visible = [...document.querySelectorAll('body *')]
      .filter(e => e.offsetParent !== null && e.children.length === 0 && e.textContent.trim())
      .map(e => e.id || e.className || e.tagName);
    return {call: document.getElementById('minimal-opening-call').textContent,
            conf: document.getElementById('minimal-opening-confidence').textContent, visible};
  }, [i, usable, dir]);
  const base = {ticker: 'KXBTC15M-T', opens_at: open, closes_at: close};
  const cal = {status: 'EXPERIMENTAL CALIBRATED CANDIDATE'};
  const out = {
    up: await run({...base, up: 0.74, down: 0.26, confidence: cal, decision_policy: {threshold: 0.7}}, true, 'UP'),
    down: await run({...base, up: 0.22, down: 0.78, confidence: cal, decision_policy: {threshold: 0.7}}, true, 'DOWN'),
    wait: await run({...base, up: 0.58, down: 0.42, confidence: cal, decision_policy: {threshold: 0.7}}, false, 'WAIT'),
    fallback: await run({...base, up: 0.55, down: 0.45, confidence: {status: 'UNCALIBRATED'}}, false, 'WAIT'),
    closed: await run({...base, closes_at: new Date(now - 1).toISOString(), up: 0.9, down: 0.1, confidence: cal}, true, 'UP'),
    other_contract: await run({...base, ticker: 'KXBTC15M-OLD', up: 0.9, down: 0.1, confidence: cal}, true, 'UP'),
  };
  await page.evaluate(() => expired('test outage'));
  out.outage = {call: await page.textContent('#minimal-opening-call'), conf: await page.textContent('#minimal-opening-confidence')};
  out.errors = errors;
  console.log(JSON.stringify(out));
  await b.close();
})();
"""


def _node_with_playwright():
    node = shutil.which("node")
    if not node:
        return None
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True,
                           env={**shutil.os.environ, "NODE_PATH": root})
    return (node, root) if probe.returncode == 0 else None


def test_terminal_behaviour_in_a_real_browser(tmp_path):
    found = _node_with_playwright()
    if not found:
        pytest.skip("Node Playwright is not available")
    node, root = found
    script = tmp_path / "terminal.js"
    script.write_text(NODE_SCRIPT)
    result = subprocess.run(
        [node, str(script), PAGE.as_uri()], capture_output=True, text=True, timeout=120,
        env={**shutil.os.environ, "NODE_PATH": root},
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert (out["up"]["call"], out["up"]["conf"]) == ("UP", "74%")
    assert (out["down"]["call"], out["down"]["conf"]) == ("DOWN", "78%")
    assert (out["wait"]["call"], out["wait"]["conf"]) == ("WAIT", "58%")
    assert (out["fallback"]["call"], out["fallback"]["conf"]) == ("WAIT", "—")
    for stale in ("closed", "other_contract", "outage"):
        assert (out[stale]["call"], out[stale]["conf"]) == ("WAIT", "—"), stale
    visible = set(out["up"]["visible"])
    assert visible <= {"", "H1", "minimal-opening-call", "minimal-opening-confidence",
                       "term-call-qualifier", "term-confidence-qualifier",
                       "primary-contract", "primary-time", "score-correct", "score-wrong",
                       "score-accuracy", "score-sample", "score-title", "score-acc", "score-line",
                       "I", "ai-lock-note", "ai1-line", "ai2-line", "ai3-line"} | {
        v for v in visible if v.startswith(("green", "red", "amber", "term-qual"))
    }

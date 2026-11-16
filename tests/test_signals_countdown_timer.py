"""Top TIME LEFT countdown: anchored to the contract's closes_at, MM:SS, 1 s ticks."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "src" / "signals_display" / "dashboard.html"


def test_top_timer_uses_contract_expiry_not_server_snapshot():
    html = PAGE.read_text(encoding="utf-8")
    assert "txt('primary-time',countdown(" not in html
    assert html.count("syncContractClock(s);") == 1
    assert "renderContractClock();" in html
    assert "Date.parse(s.market?.closes_at)" in html
    assert "contractClock.offsetMs=serverNow-Date.now()" in html


NODE_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const page = await b.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/**', r => r.abort());
  const load = async () => { await page.goto(process.argv[2]); await page.waitForTimeout(200); };
  const top = () => page.textContent('#primary-time');
  await load();
  const out = {};
  out.format = await page.evaluate(() => {
    const n = 1_800_000_000_000;
    return [900000, 899001, 65000, 500, 0, -1000, 905000, NaN]
      .map(d => contractCountdownText(n + d, n));
  });
  // Server clock 3 hours ahead of the browser: the countdown must still be right.
  out.skew = await page.evaluate(() => {
    const server = Date.now() + 3 * 3600e3;
    syncContractClock({generated_at: new Date(server).toISOString(),
                       market: {ticker: 'A', closes_at: new Date(server + 600e3).toISOString()}});
    return document.getElementById('primary-time').textContent;
  });
  // Rollover: an ending contract, then the next one begins.
  out.rollover = await page.evaluate(() => {
    const now = Date.now(), iso = t => new Date(t).toISOString();
    syncContractClock({generated_at: iso(now), market: {ticker: 'A', closes_at: iso(now + 3e3)}});
    const before = document.getElementById('primary-time').textContent;
    syncContractClock({generated_at: iso(now), market: {ticker: 'B', closes_at: iso(now + 900e3)}});
    return [before, document.getElementById('primary-time').textContent];
  });
  // Delayed transition: contract expired and the next one is not known yet.
  out.expired = await page.evaluate(() => {
    const now = Date.now(), iso = t => new Date(t).toISOString();
    syncContractClock({generated_at: iso(now), market: {ticker: 'C', closes_at: iso(now - 2e3)}});
    return document.getElementById('primary-time').textContent;
  });
  // A state without market data (interruption) keeps the last real expiry.
  out.interruption = await page.evaluate(() => {
    const now = Date.now(), iso = t => new Date(t).toISOString();
    syncContractClock({generated_at: iso(now), market: {ticker: 'D', closes_at: iso(now + 300e3)}});
    syncContractClock({generated_at: iso(now), stale: true});
    return document.getElementById('primary-time').textContent;
  });
  // Ticks every second from the expiry, even while no new state arrives.
  await page.evaluate(() => {
    const now = Date.now(), iso = t => new Date(t).toISOString();
    lastState = {remaining_seconds: 30};  // deliberately wrong snapshot
    syncContractClock({generated_at: iso(now), market: {ticker: 'E', closes_at: iso(now + 120e3)}});
  });
  const t0 = await top();
  await page.waitForTimeout(2300);
  out.tick = [t0, await top()];
  // Browser refresh: a fresh page re-anchors from the first state immediately.
  await load();
  out.refresh = await page.evaluate(() => {
    const now = Date.now(), iso = t => new Date(t).toISOString();
    lastState = {remaining_seconds: 30};
    syncContractClock({generated_at: iso(now), market: {ticker: 'F', closes_at: iso(now + 420e3)}});
    return document.getElementById('primary-time').textContent;
  });
  await page.waitForTimeout(1300);
  out.refresh_after_tick = await top();
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
    env = {**shutil.os.environ, "NODE_PATH": root}
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, env=env)
    return (node, env) if probe.returncode == 0 else None


def test_countdown_accuracy_rollover_refresh_in_a_real_browser(tmp_path):
    found = _node_with_playwright()
    if not found:
        pytest.skip("Node Playwright is not available")
    node, env = found
    script = tmp_path / "countdown.js"
    script.write_text(NODE_SCRIPT)
    result = subprocess.run([node, str(script), PAGE.as_uri()], capture_output=True,
                            text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["errors"] == []
    assert out["format"] == ["15:00", "15:00", "01:05", "00:01", "00:00", "--:--", "--:--", "--:--"]
    assert out["skew"] in {"10:00", "09:59"}
    assert out["rollover"][0] in {"00:03", "00:02"} and out["rollover"][1] in {"15:00", "14:59"}
    assert out["expired"] == "--:--"
    assert out["interruption"] in {"05:00", "04:59"}
    start, later = out["tick"]
    to_seconds = lambda text: int(text[:2]) * 60 + int(text[3:])
    assert start in {"02:00", "01:59"} and 1 <= to_seconds(start) - to_seconds(later) <= 3
    assert out["refresh"] in {"07:00", "06:59"}
    # The 1 s interval re-renders from closes_at, not the wrong 30 s snapshot.
    assert out["refresh_after_tick"] in {"06:59", "06:58"}


PILL_SCRIPT = r"""
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch(); const out = {};
  for (const [vp, size] of [['desktop', {width: 1440, height: 900}], ['mobile', {width: 390, height: 844}]]) {
    const p = await b.newPage({viewport: size});
    await p.route('**/api/**', r => r.abort());
    await p.goto(process.argv[2]); await p.waitForTimeout(200);
    out[vp] = await p.evaluate(() => {
      const now = Date.now(), iso = t => new Date(t).toISOString();
      syncContractClock({generated_at: iso(now), market: {ticker: 'KXBTC15M-T', closes_at: iso(now + 754e3)}});
      const cards = () => [...document.querySelectorAll('#minimal-opening .minimal-value')]
        .map(c => { const r = c.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.left)]; });
      const pill = document.querySelector('main>div.primary-contract'), time = document.getElementById('primary-time');
      const box = pill.getBoundingClientRect(), withPill = cards();
      pill.style.setProperty('display', 'none', 'important');
      const withoutPill = cards();
      pill.style.removeProperty('display');
      return {visible: time.offsetParent !== null && getComputedStyle(pill).display !== 'none',
              text: time.textContent, pillBottom: box.bottom,
              firstCardTop: withPill[0][0], cardsUnmoved: JSON.stringify(withPill) === JSON.stringify(withoutPill),
              hscroll: document.documentElement.scrollWidth > innerWidth};
    });
    await p.close();
  }
  console.log(JSON.stringify(out)); await b.close();
})();
"""


def test_top_countdown_is_visible_above_cards_without_moving_them(tmp_path):
    found = _node_with_playwright()
    if not found:
        pytest.skip("Node Playwright is not available")
    node, env = found
    script = tmp_path / "pill.js"
    script.write_text(PILL_SCRIPT)
    result = subprocess.run([node, str(script), PAGE.as_uri()], capture_output=True,
                            text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    for viewport, view in out.items():
        assert view["visible"], viewport
        assert view["text"] in {"12:34", "12:33"}, viewport
        assert view["pillBottom"] <= view["firstCardTop"], viewport  # at the top, no overlap
        assert view["cardsUnmoved"], viewport  # the pill takes no layout space
        assert not view["hscroll"], viewport

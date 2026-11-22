"""The opening card tells a lost local connection apart from stale production data and shows input drift."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parents[1] / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
MARK = "Connection and model-input status for the opening card"


def _block():
    start = HTML.index(MARK)
    start = HTML.rindex("<script>", 0, start) + len("<script>")
    return HTML[start:HTML.index("</script>", start)]


def test_status_lines_live_inside_the_visible_opening_card():
    card = HTML[HTML.index('id="minimal-opening-card"'):]
    card = card[:card.index("</article>")]
    assert 'id="term-link"' in card and 'id="term-drift"' in card
    block = _block()
    assert "innerHTML" not in block and "textContent" in block       # text nodes only
    assert "minimal-opening-call" not in block                       # never touches the call itself


HARNESS = r"""
const els = {};
function el(id){ return els[id] || (els[id] = {id, textContent:'', hidden:true, attrs:{}, setAttribute(k,v){this.attrs[k]=v}}); }
global.document = { getElementById: el };
global.performance = { now: () => clock };
let clock = 0; const timers = [];
global.setTimeout = (f, ms) => 0; global.clearTimeout = () => {};
global.setInterval = (f, ms) => { timers.push(f); return 0; };
global.AbortController = class { constructor(){ this.signal = {}; } abort(){} };
let mode = SCENARIO.mode;
global.lastState = SCENARIO.stale ? { stale: true } : { stale: false };
global.fetch = (url) => {
  if (url.startsWith('/api/accuracy-health')) return Promise.resolve({ ok: true, json: () => Promise.resolve(SCENARIO.health) });
  if (mode === 'down') return Promise.reject(new Error('net'));
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ state_version: 5, published_at: SCENARIO.published }) });
};
BLOCK
(async () => {
  for (let i = 0; i < SCENARIO.beats; i++) { clock += SCENARIO.step; timers[0](); await new Promise(r => setImmediate(r)); await new Promise(r => setImmediate(r)); }
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({ link: els['term-link'].textContent, tone: els['term-link'].attrs['data-tone'],
                               drift: els['term-drift'].textContent, driftHidden: els['term-drift'].hidden }));
})();
"""


def _run(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = HARNESS.replace("BLOCK", _block()).replace("SCENARIO", json.dumps(scenario))
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


HEALTHY = {"predictive_edge": {"input_drift": {"status": "WITHIN TRAINING RANGE"}}}
DRIFTED = {"predictive_edge": {"input_drift": {"status": "DRIFTED", "mean_abs_standardized_shift": 0.84,
                                               "largest": [{"input": "advanced_trade_intensity", "shift_sd": 3.04}]}}}


def test_unreachable_server_is_reported_as_a_local_connection_problem():
    r = _run({"mode": "down", "stale": False, "published": "x", "beats": 3, "step": 5000, "health": HEALTHY})
    assert r["tone"] == "red" and "SSH tunnel" in r["link"] and "production keeps running" in r["link"]


def test_reachable_server_with_stale_data_is_not_blamed_on_the_tunnel():
    r = _run({"mode": "up", "stale": True, "published": "x", "beats": 2, "step": 5000, "health": HEALTHY})
    assert r["tone"] == "red" and "PRODUCTION MARKET DATA STALE" in r["link"] and "not your connection" in r["link"]


def test_frozen_publication_counts_as_stale_even_if_the_flag_is_not_set():
    r = _run({"mode": "up", "stale": False, "published": "same", "beats": 8, "step": 5000, "health": HEALTHY})
    assert "PRODUCTION MARKET DATA STALE" in r["link"]


def test_live_connection_and_drift_warning():
    r = _run({"mode": "up", "stale": False, "published": "x", "beats": 1, "step": 1000, "health": DRIFTED})
    assert r["tone"] == "green" and "LIVE" in r["link"]
    assert r["driftHidden"] is False and "advanced_trade_intensity 3.04 SD" in r["drift"]
    r = _run({"mode": "up", "stale": False, "published": "x", "beats": 1, "step": 1000, "health": HEALTHY})
    assert r["driftHidden"] is True

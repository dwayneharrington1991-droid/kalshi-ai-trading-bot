"""Exercise status rendering without a browser or network."""
from pathlib import Path
import shutil
import subprocess

import pytest

from src.signals_display.server import dashboard_page


def test_dashboard_includes_all_ai_panels_and_asset():
    body = dashboard_page()
    for marker in (b'opening-ai-status', b'scalp-ai-status', b'combined-ai-status', b'/ai_status.js'):
        assert body.count(marker) == 1


def test_ai_labels_fail_closed_on_stale_missing_and_previous_contract():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed to execute the browser status script')
    asset = Path(__file__).resolve().parents[1] / 'src/signals_display/ai_status.js'
    script = r"""
const vm = require('vm'), fs = require('fs'), assert = require('assert');
let tick;
const nodes = {};
const context = {
  document: {getElementById: id => nodes[id] ||= {textContent: ''}},
  setInterval: callback => { tick = callback; },
  Date, lastState: null, lastSuccess: Date.now()
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert(nodes['opening-ai-status'].textContent.startsWith('WAIT'));
context.lastState = {remaining_seconds: 600, market: {ticker: 'new'}, prediction_engine: {initial: {ticker:'new', up:0.65}}};
tick();
assert(nodes['opening-ai-status'].textContent.includes('UNVALIDATED'));
assert(nodes['scalp-ai-status'].textContent.includes('NO VERIFIED EDGE'));
context.lastState.prediction_engine.initial.ticker = 'old';
tick();
assert(nodes['opening-ai-status'].textContent.startsWith('WAIT'));
context.lastState.stale = true;
tick();
assert(nodes['scalp-ai-status'].textContent.startsWith('WAIT'));
context.lastState.stale = false;
context.lastSuccess = Date.now() - 6000;
tick();
assert(nodes['scalp-ai-status'].textContent.startsWith('WAIT'));
assert(nodes['combined-ai-status'].textContent === 'WAIT · NO VERIFIED EDGE');
"""
    subprocess.run([node, '-e', script, str(asset)], check=True, capture_output=True, text=True)

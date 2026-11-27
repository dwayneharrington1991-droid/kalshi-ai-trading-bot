"""Three-model comparison panel: collapsible, below the five lines, COLLECTING DATA until results exist."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")


def test_panel_is_collapsible_and_after_the_five_lines():
    top = HTML[HTML.index('<section id="minimal-opening"'):]
    assert top.index('id="ai-three-lines"') < top.index('id="of1-card"')
    assert '<details id="of1-details"><summary>AI ACCURACY RESEARCH — THREE-MODEL COMPARISON</summary>' in HTML
    assert 'route == "/api/of1"' in (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")


def _run(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    start = HTML.index("var det=document.getElementById('of1-details')")
    block = HTML[HTML.rindex("<script>", 0, start) + 8:HTML.index("</script>", start)]
    stub = r"""
function El(){this.textContent='';this.innerHTML='';this.open=true;this.offsetParent={};}
El.prototype.addEventListener=function(t,f){if(t==='toggle')__toggle=f;};
var ids={}; ['of1-details','of1-status','of1-targets'].forEach(function(i){ids[i]=new El();});
var document={hidden:false,getElementById:function(i){return ids[i];}};
var __toggle=null; var setInterval=function(){}; var AbortSignal={timeout:function(){return null;}};
"""
    src = stub + "var fetch=function(){return Promise.resolve({json:function(){return " + json.dumps(payload) + ";}});};" + block + \
        ";__toggle();setTimeout(function(){console.log(JSON.stringify({s:ids['of1-status'].textContent,h:ids['of1-targets'].innerHTML}));},50);"
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_collecting_data_and_results_render():
    r = _run({"status": "UNAVAILABLE", "reason": "of1_status.json not written yet"})
    assert r["s"].startswith("COLLECTING DATA")
    r = _run({"test": "OF1", "generated_at": "2026-11-27T06:00:00Z", "prospective_start": "2026-11-27T03:15:00Z",
              "targets": {"first6": {"title": "FIRST 6 MIN", "status": "COLLECTING DATA", "records": 12, "excluded": 1, "models": {}}}})
    assert "COLLECTING DATA · 12 point-in-time records, 1 excluded" in r["h"]
    m = {"label": "AI B — + walls <x>", "correct": 60, "wrong": 40, "wait": 50, "accuracy": 0.6, "coverage": 0.667,
         "brier": 0.2401, "log_loss": 0.67, "vs_ai_a_ci95": [-0.01, 0.002]}
    r = _run({"targets": {"opening_final": {"title": "Opening", "status": "RESEARCH ONLY — NOT VALIDATED", "scored": 150,
                                            "excluded": 3, "models": {"AI_B_walls": m}}}})
    assert "<td>60</td><td>40</td><td>50</td><td>60.0%</td><td>66.7%</td><td>0.2401</td>" in r["h"]
    assert "&lt;x&gt;" in r["h"] and "<x>" not in r["h"] and "-0.0100 to 0.0020" in r["h"]

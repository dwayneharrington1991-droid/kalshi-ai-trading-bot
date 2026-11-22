"""Experimental consensus: one rule shared by the dashboard endpoint and the Telegram service; cards labelled."""
import importlib.util
from pathlib import Path

from src.signals_display.consensus import combine

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dual", ROOT / "scripts" / "dual_ai_alerts.py")
dual = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dual)


def test_rule():
    assert combine(0.60, 0.58)["combined"] == "UP" and combine(0.40, 0.42)["combined"] == "DOWN"
    assert combine(0.70, 0.54)["combined"] == "WAIT" and combine(0.62, 0.40)["combined"] == "WAIT"
    down = combine(0.40, 0.30)
    assert abs(down["combined_confidence"] - 0.65) < 1e-12 and down["combined_passes_threshold"]
    assert combine(0.5, 0.5)["combined_confidence"] is None and "NOT VALIDATED" in combine(0.5, 0.5)["combined_label"]


def test_dashboard_and_service_use_the_same_rule():
    grid = [i / 100 for i in range(20, 81)]
    for a in grid:
        for b in grid:
            c, d = dual.combine(a, b)
            r = combine(a, b)
            assert d == r["combined"] and abs(c - r["combined_up"]) < 1e-12


def test_three_cards_visible_and_labelled():
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    sec = html[html.index('<section id="minimal-opening"'):html.index("</section>", html.index('<section id="minimal-opening"'))]
    assert "#challenger-card,#consensus-card{display:none!important}" in html   # superseded by the three-line strip
    for ident, badge in (("minimal-opening-card", "AI #1 — LIVE (official)"),
                         ("challenger-card", "AI #2 — CHALLENGER (NOT VALIDATED)"),
                         ("consensus-card", "COMBINED AI — CONSENSUS (EXPERIMENTAL, NOT VALIDATED)")):
        assert f'id="{ident}"' in sec and badge in sec
    assert "<h1>AI OPENING CALL</h1>" in sec
    start = html.index("Challenger AI card: reads only /api/challenger-opening")
    block = html[start:html.index("</script>", start)]
    assert "innerHTML" not in block and "combined_passes_threshold" in block and "left" in block


def _run_strip(payload):
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    start = html.index("Three-line top strip")
    block = html[html.rindex("<script>", 0, start) + 8:html.index("</script>", start)]
    src = ("var els={};['ai1-line','ai2-line','ai3-line'].forEach(function(i){els[i]={textContent:'',className:''};});"
           "var document={getElementById:function(i){return els[i];}};var setInterval=function(){};"
           f"var fetch=function(){{return Promise.resolve({{ok:true,json:function(){{return {json.dumps(payload)};}}}});}};"
           + block + ";setTimeout(function(){console.log(JSON.stringify(els));},20);")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_three_line_strip_shows_real_calls_and_wait_when_missing():
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    top = html[html.index('<section id="minimal-opening"'):]
    assert top.index('id="ai-three-lines"') < top.index('id="minimal-opening-card"')
    assert ".ai-line.combined{font-size:2em" in html
    r = _run_strip({"status": "READY", "closes_at": "2999-01-01T00:00:00Z", "live_up": 0.62, "live_direction": "UP",
                    "up": 0.40, "direction": "DOWN", "combined_up": 0.51, "combined": "WAIT"})
    assert r["ai1-line"]["textContent"] == "UP — 62.0%" and r["ai1-line"]["className"] == "up"
    assert r["ai2-line"]["textContent"] == "DOWN — 60.0%" and r["ai3-line"]["textContent"] == "WAIT — 51.0% UP"
    r = _run_strip({"status": "UNAVAILABLE", "closes_at": "2999-01-01T00:00:00Z"})
    assert all(r[k]["textContent"] == "WAIT" for k in ("ai1-line", "ai2-line", "ai3-line"))

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
    assert "#challenger-card,#consensus-card," in html                       # superseded by the four-line strip
    for ident, badge in (("minimal-opening-card", "AI #1 — LIVE (official)"),
                         ("challenger-card", "AI #2 — CHALLENGER (NOT VALIDATED)"),
                         ("consensus-card", "COMBINED AI — CONSENSUS (EXPERIMENTAL, NOT VALIDATED)")):
        assert f'id="{ident}"' in sec and badge in sec
    assert "<h1>AI OPENING CALL</h1>" in sec
    start = html.index("Challenger AI card: reads only /api/challenger-opening")
    block = html[start:html.index("</script>", start)]
    assert "innerHTML" not in block and "combined_passes_threshold" in block and "left" in block


def _run_strip(challenger, minute6=None):
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    start = html.index("Four-line top strip")
    block = html[html.rindex("<script>", 0, start) + 8:html.index("</script>", start)]
    ids = ["ai1-line", "ai2-line", "ai3-line", "ai4-line", "ai4-row", "ai-lock-note", "ai4-note"]
    src = ("var els={};" + json.dumps(ids) + ".forEach(function(i){els[i]={textContent:'',className:'',hidden:true};});"
           "var document={getElementById:function(i){return els[i];}};var setInterval=function(){};"
           "var P={'/api/challenger-opening':" + json.dumps(challenger) + ",'/api/minute6':" + json.dumps(minute6 or {}) + "};"
           "var fetch=function(u){return Promise.resolve({ok:true,json:function(){return P[u];}});};"
           + block + ";setTimeout(function(){console.log(JSON.stringify(els));},30);")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_four_line_strip_shows_server_lines_and_minute6_only_when_valid():
    from src.signals_display import four_lines
    html = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")
    top = html[html.index('<section id="minimal-opening"'):]
    assert top.index('id="ai-three-lines"') < top.index('id="minimal-opening-card"')
    assert ".ai-line.combined{font-size:2em" in html
    assert "#minimal-opening-card,#minimal-confidence-card,#minute6-card{display:none!important}" in html
    lines = four_lines.lines(("UP", 0.62), ("DOWN", 0.40), ("WAIT", 0.51))
    ch = {"status": "READY", "ticker": "KX-Z", "closes_at": "2999-01-01T00:00:00Z",
          "generated_from": "2999-01-01T00:00:02Z", "lines": lines}
    m6 = {"status": "LOCKED", "valid": True, "ticker": "KX-Z", "locked_at": "2999-01-01T00:06:07Z",
          "line": four_lines.minute6_line("DOWN", 0.03)}
    r = _run_strip(ch, m6)
    assert [r[f"ai{k}-line"]["textContent"] for k in (3, 1, 2)] == ["COMBINED CALL: WAIT 🟡 — 51% UP", "AI #1: UP 🟢 — 62%", "AI #2: DOWN 🔴 — 60%"]
    top_html = html[html.index('id="ai-three-lines"'):]
    assert top_html.index('id="ai3-line"') < top_html.index('id="ai1-line"') < top_html.index('id="ai2-line"')
    assert r["ai1-line"]["className"] == "up" and r["ai2-line"]["className"] == "down"
    assert r["ai4-row"]["hidden"] is False and r["ai4-line"]["textContent"] == "MINUTE-6: DOWN 🔴 — 97% | WAIT"
    other = dict(m6, ticker="KX-OLD")                                   # minute-6 of another market: hidden
    assert _run_strip(ch, other)["ai4-row"]["hidden"] is True
    assert _run_strip(ch, dict(m6, valid=False))["ai4-row"]["hidden"] is True
    r = _run_strip({"status": "UNAVAILABLE", "closes_at": "2999-01-01T00:00:00Z"})
    assert [r[f"ai{k}-line"]["textContent"] for k in (3, 1, 2)] == ["COMBINED CALL: WAIT 🟡 — N/A", "AI #1: WAIT 🟡 — N/A", "AI #2: WAIT 🟡 — N/A"]
    expired = dict(ch, closes_at="2000-01-01T00:00:00Z")
    assert _run_strip(expired, m6)["ai1-line"]["textContent"] == "AI #1: WAIT 🟡 — N/A"

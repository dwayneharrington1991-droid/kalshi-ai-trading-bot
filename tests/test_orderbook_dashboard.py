"""Dashboard order-book panel: visible section, polls only when open/visible, renders real collector output."""
import json
import shutil
import subprocess
import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.signals_display import orderbook_live as ob

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "src/signals_display/dashboard.html").read_text(encoding="utf-8")


def test_section_exists_after_the_five_lines_and_polls_only_when_open():
    top = HTML[HTML.index('<section id="minimal-opening"'):]
    assert top.index('id="ai-three-lines"') < top.index('id="orderbook-card"')
    assert "LIVE BTC ORDER BOOK — BUY &amp; SELL WALLS" in HTML
    start = HTML.index("Live BTC order book panel: reads only /api/orderbook")
    block = HTML[start:HTML.index("</script>", start)]
    assert "innerHTML" not in block
    assert "if (busy || !det.open || document.hidden || det.offsetParent === null) { return; }" in block
    assert "setInterval(load, 1000)" in block
    assert 'route == "/api/orderbook"' in (ROOT / "src/signals_display/server.py").read_text(encoding="utf-8")


def _payload():
    b = ob.Book("Kraken BTC/USD", depth=25)
    for i in range(25):
        b.apply("bid", D("60000.0") - i, D("0.5")); b.apply("ask", D("60000.1") + i, D("0.4"))
    b.apply("bid", D("59990.0"), D("9")); b.status, b.last_event, b.last_receipt = "OK", time.time(), time.time()
    v, _ = ob.depth_view(b)
    flow = ob.TradeFlow(); flow.add(time.time(), "buy", 0.3, 60000.1, "t")
    w = flow.windows(time.time())
    v.update(trades=w, cvd_btc=flow.cvd, pressure=ob.pressure(v, w), messages=10, last_error=None)
    return {"generated_at": time.time(), "status": "OK", "snapshot_age_s": 0.4, "venues": {"kraken": v, "coinbase": v}}


def test_panel_renders_ladder_walls_and_live_status_from_collector_output():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    start = HTML.index("Live BTC order book panel: reads only /api/orderbook")
    block = HTML[HTML.rindex("<script>", 0, start) + 8:HTML.index("</script>", start)]
    stub = r"""
function El(tag){this.tag=tag;this.children=[];this.textContent='';this.className='';this.style={};this.open=true;this.offsetParent={};}
El.prototype.appendChild=function(c){this.children.push(c);return c;};
El.prototype.addEventListener=function(){};El.prototype.querySelectorAll=function(){return [];};
El.prototype.getContext=function(){return new Proxy({}, {get:function(){return function(){};}, set:function(){return true;}});};
El.prototype.getAttribute=function(){return null;};
var ids={}; ['ob-details','ob-rows','ob-status','ob-pressure','ob-walls','ob-events','ob-depth','ob-venues'].forEach(function(i){ids[i]=new El('div');});
ids['ob-depth'].width=900; ids['ob-depth'].height=180;
var document={hidden:false,getElementById:function(i){return ids[i];},createElement:function(t){return new El(t);}};
var __load=null; var setInterval=function(f){__load=f;};
"""
    src = stub + "var fetch=function(){return Promise.resolve({ok:true,json:function(){return " + json.dumps(_payload()) + ";}});};" + \
        block + ";ids['ob-details'].open=true;__load();setTimeout(function(){var r=ids['ob-rows'].children;" \
        "console.log(JSON.stringify({status:ids['ob-status'].textContent,cls:ids['ob-status'].className,walls:ids['ob-walls'].textContent," \
        "row0:r[0].children.map(function(c){return c.textContent;}),wallrows:r.filter(function(x){return x.className==='wall';}).length,pres:ids['ob-pressure'].textContent}));},50);"
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.strip().splitlines()[-1])
    assert r["status"].startswith("LIVE") and r["cls"] == "live"
    assert r["row0"][3] == "60,000.00" and r["row0"][4] == "60,000.10" and r["row0"][2] == "0.5000"
    assert "BUY 9.000 BTC @ 59,990.00" in r["walls"] and r["wallrows"] == 1 and "descriptive" in r["pres"]

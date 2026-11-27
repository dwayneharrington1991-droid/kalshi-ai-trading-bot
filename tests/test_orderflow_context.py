"""Order-flow Telegram context: labelled, never a prediction, UNAVAILABLE when stale."""
import json
import time

from src.signals_display import orderflow_context as oc


def _data(cb="BUY", kr="BUY", cb_status="LIVE", kr_status="LIVE", age=0.5):
    v = lambda st, lab: {"status": st, "pressure": {"label": lab}, "bands": {}, "trades": {}, "walls": []}
    return {"generated_at": time.time() - age, "venues": {"coinbase": v(cb_status, cb), "kraken": v(kr_status, kr)}}


def test_line_agreement_partial_and_unavailable(tmp_path):
    assert oc.line(_data()) .endswith("BUY PRESSURE — Coinbase BUY · Kraken BUY — FRESH")
    assert "SELL PRESSURE" in oc.line(_data("SELL", "SELL"))
    assert "MIXED — Coinbase BUY · Kraken SELL" in oc.line(_data("BUY", "SELL"))
    assert "PARTIAL" in oc.line(_data(kr_status="STALE")) and "Kraken" not in oc.line(_data(kr_status="STALE")).split("—")[1]
    assert "UNAVAILABLE" in oc.line(_data(cb_status="STALE", kr_status="RESYNC"))
    assert "UNAVAILABLE" in oc.line(None) and "not a prediction" in oc.line(None)
    f = tmp_path / "ob.json"
    f.write_text(json.dumps(_data(age=30)))
    assert oc.load(f) is None                                            # stale snapshot is never used
    f.write_text(json.dumps(_data(age=1)))
    assert oc.load(f) is not None


def test_messages_carry_the_context_line_without_changing_the_five_lines(tmp_path, monkeypatch):
    import importlib.util
    from datetime import datetime, timezone
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("d", root / "scripts" / "dual_ai_alerts.py")
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    monkeypatch.setattr(oc, "LATEST", tmp_path / "missing.json")
    monkeypatch.setattr(oc.load, "__defaults__", (tmp_path / "missing.json", None))
    t = datetime(2026, 11, 27, 3, 0, 2, tzinfo=timezone.utc)
    msg = d.message("READY", {"ticker": "KX", "opens": t, "closes": t, "generated": t, "live": "UP", "live_up": 0.6,
                              "challenger": "WAIT", "challenger_up": 0.52, "combined": "WAIT", "combined_up": 0.56})
    lines = msg.split("\n")
    assert lines[1].startswith("COMBINED CALL:") and lines[5].startswith("MINUTE-6 FINAL:")
    assert lines[6] == "ORDER FLOW (context, not a prediction): UNAVAILABLE — collector data stale or missing"

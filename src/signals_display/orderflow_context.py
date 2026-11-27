"""Order-flow context for Telegram and point-in-time feature snapshots for AI B / AI C research (stdlib only).

Reads the order-book collector's ~/dual-ai-alerts/orderbook_latest.json. This is CONTEXT, never a prediction:
it never changes a locked call. A venue counts only if its book is LIVE and the snapshot is fresh; otherwise the
line says UNAVAILABLE / STALE instead of guessing.
"""
import json
import time
from pathlib import Path

LATEST = Path.home() / "dual-ai-alerts" / "orderbook_latest.json"
MAX_AGE_S = 5.0
NAMES = {"coinbase": "Coinbase", "kraken": "Kraken"}


def load(path=LATEST, now=None):
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    now = now or time.time()
    if now - float(d.get("generated_at") or 0) > MAX_AGE_S:
        return None
    return d


def line(data):
    """'ORDER FLOW (context, not a prediction): BUY PRESSURE — Coinbase BUY · Kraken BUY — FRESH'."""
    head = "ORDER FLOW (context, not a prediction): "
    if not data:
        return head + "UNAVAILABLE — collector data stale or missing"
    labels = {}
    for key, name in NAMES.items():
        v = (data.get("venues") or {}).get(key) or {}
        if v.get("status") == "LIVE":
            labels[name] = (v.get("pressure") or {}).get("label") or "MIXED"
    if not labels:
        return head + "UNAVAILABLE — no exchange book is live"
    vals = set(labels.values())
    overall = "BUY PRESSURE" if vals == {"BUY"} else "SELL PRESSURE" if vals == {"SELL"} else "MIXED"
    detail = " · ".join(f"{n} {l}" for n, l in labels.items())
    fresh = "FRESH" if len(labels) == len(NAMES) else "PARTIAL (one exchange stale)"
    return f"{head}{overall} — {detail} — {fresh}"


def features(data, decision_ts=None):
    """Compact point-in-time feature snapshot (only venues LIVE at the snapshot), for later chronological research.
    Every value was already received when the collector wrote its latest second (no future data)."""
    if not data:
        return None
    out = {"snapshot_at": data.get("generated_at"), "decision_ts": decision_ts, "venues": {}}
    for key in NAMES:
        v = (data.get("venues") or {}).get(key) or {}
        if v.get("status") != "LIVE":
            out["venues"][key] = {"status": v.get("status") or "MISSING"}
            continue
        walls = v.get("walls") or []
        bw = max((w for w in walls if w["side"] == "bid"), key=lambda w: w["size_btc"], default=None)
        aw = max((w for w in walls if w["side"] == "ask"), key=lambda w: w["size_btc"], default=None)
        bands = v.get("bands") or {}
        tr = v.get("trades") or {}
        out["venues"][key] = {
            "status": "LIVE", "event_age_s": v.get("age_s"), "mid": v.get("mid"), "spread_bps": v.get("spread_bps"),
            "imb_005": (bands.get("0.05%") or {}).get("imbalance"), "imb_010": (bands.get("0.10%") or {}).get("imbalance"),
            "imb_025": (bands.get("0.25%") or {}).get("imbalance"), "imb_050": (bands.get("0.50%") or {}).get("imbalance"),
            "bid_025": (bands.get("0.25%") or {}).get("bid_btc"), "ask_025": (bands.get("0.25%") or {}).get("ask_btc"),
            "bid_wall_btc": bw and bw["size_btc"], "bid_wall_dist_pct": bw and bw["distance_pct"],
            "ask_wall_btc": aw and aw["size_btc"], "ask_wall_dist_pct": aw and aw["distance_pct"],
            "wall_events": len(v.get("wall_events") or []),
            "cvd_btc": v.get("cvd_btc"),
            **{f"buy_{w}": (tr.get(w) or {}).get("buy_btc") for w in ("5s", "15s", "60s", "300s")},
            **{f"sell_{w}": (tr.get(w) or {}).get("sell_btc") for w in ("5s", "15s", "60s", "300s")},
            **{f"trades_{w}": (tr.get(w) or {}).get("trades") for w in ("15s", "60s")},
            "pressure": (v.get("pressure") or {}).get("label")}
    return out

"""Time-boxed Telegram alert with BOTH frozen opening calls (live AI + v4 challenger), one per market opening.

Runs beside the server (never inside it) and reads only the read-only HTTP API on 127.0.0.1:8768, so it holds
no journal file open and cannot change the live call, the existing alerts, the model, records or orders.
It reuses the existing Telegram bot and chat via phone_alerts.load_config / send (no new credentials).

Rules:
  * a message is sent only for a genuine frozen opening: the challenger endpoint and /api/state must name the
    SAME ticker, the challenger's reproduced live P(UP) must equal the state's live P(UP), the record must be
    generated within GENERATED_MAX_S of the open, and the market must still have > OUTCOME_GUARD_S to run;
  * if the challenger is unavailable for a genuine live call, the message says so (nothing is substituted);
  * one message per ticker: claimed in a local SQLite table (PRIMARY KEY ticker) before sending;
  * the run ends by itself after --hours.

  python scripts/dual_ai_alerts.py --release /home/opc/kalshi-preview-repair-<sha7> --db ~/dual-ai-alerts/sent.sqlite3
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

BASE = "http://127.0.0.1:8768"
THRESHOLD = 0.55
GENERATED_MAX_S = 150          # opening record must be made within 150 s of the market open
CHALLENGER_WAIT_S = 90         # how long after the open to wait for the challenger before reporting it unavailable
OUTCOME_GUARD_S = 60           # never send in the last minute of a market
LABEL = "CHALLENGER is EXPERIMENTAL and NOT VALIDATED (research only). LIVE AI is the official call."


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def direction(p):
    if not isinstance(p, (int, float)):
        return None
    return "UP" if p >= THRESHOLD else "DOWN" if p <= 1 - THRESHOLD else "WAIT"


def fetch_json(path, base=BASE):
    with urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def opening(state, challenger, now):
    """Return (kind, details): kind in READY, CHALLENGER_UNAVAILABLE, WAITING, SKIP."""
    initial = ((state or {}).get("prediction_engine") or {}).get("initial") or {}
    ticker, live = initial.get("ticker"), initial.get("up")
    generated, closes = _ts(initial.get("generated_at")), _ts(initial.get("closes_at") or (challenger or {}).get("closes_at"))
    if not ticker or not isinstance(live, (int, float)) or not generated or not closes:
        return "SKIP", {"reason": "no frozen live opening call"}
    opens = closes - timedelta(minutes=15)
    if not timedelta(0) <= generated - opens <= timedelta(seconds=GENERATED_MAX_S):
        return "SKIP", {"ticker": ticker, "reason": "live record not made at the open"}
    if now >= closes - timedelta(seconds=OUTCOME_GUARD_S):
        return "SKIP", {"ticker": ticker, "reason": "too close to settlement"}
    d = {"ticker": ticker, "opens": opens, "closes": closes, "generated": generated,
         "live_up": float(live), "live": direction(live)}
    ch = challenger or {}
    same = ch.get("ticker") == ticker and isinstance(ch.get("live_up"), (int, float)) \
        and abs(ch["live_up"] - live) < 1e-9 and _ts(ch.get("generated_from")) == generated
    if ch.get("status") == "READY" and same and isinstance(ch.get("up"), (int, float)):
        return "READY", {**d, "challenger_up": float(ch["up"]), "challenger": direction(ch["up"])}
    if now - opens < timedelta(seconds=CHALLENGER_WAIT_S):
        return "WAITING", d
    reason = ch.get("reason") or ("challenger is for a different market" if ch.get("ticker") != ticker
                                  else f"challenger status {ch.get('status')}")
    return "CHALLENGER_UNAVAILABLE", {**d, "challenger_reason": str(reason)[:120]}


def message(kind, d):
    hm = lambda t: t.strftime("%H:%M UTC")
    lines = ["KALSHI BTC 15m - BOTH AI OPENING CALLS", d["ticker"],
             f"Opened {hm(d['opens'])} - Expires {hm(d['closes'])}",
             f"LIVE AI: {d['live']} - P(UP) {d['live_up'] * 100:.1f}%"]
    if kind == "READY":
        lines += [f"CHALLENGER AI: {d['challenger']} - P(UP) {d['challenger_up'] * 100:.1f}%",
                  f"The two AIs {'AGREE' if d['challenger'] == d['live'] else 'DISAGREE'}"]
    else:
        lines += [f"CHALLENGER AI: UNAVAILABLE ({d['challenger_reason']}) - no call substituted",
                  "Comparison unavailable"]
    lines += [LABEL,
              f"Both calls frozen {d['generated'].strftime('%H:%M:%S UTC')} for this same market, before the outcome.",
              "Signal only - place any trade manually."]
    return "\n".join(lines)


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS sent(ticker TEXT PRIMARY KEY, claimed_at TEXT, kind TEXT, "
                        "live TEXT, live_up REAL, challenger TEXT, challenger_up REAL, status TEXT, "
                        "message_id INTEGER, error TEXT, text TEXT)")
        self.db.commit()

    def claim(self, kind, d, text, now):
        cur = self.db.execute("INSERT OR IGNORE INTO sent(ticker, claimed_at, kind, live, live_up, challenger, "
                              "challenger_up, status, text) VALUES (?,?,?,?,?,?,?,'CLAIMED',?)",
                              (d["ticker"], now.isoformat(), kind, d["live"], d["live_up"], d.get("challenger"),
                               d.get("challenger_up"), text))
        self.db.commit()
        return cur.rowcount == 1

    def result(self, ticker, status, message_id=None, error=None):
        self.db.execute("UPDATE sent SET status=?, message_id=?, error=? WHERE ticker=?", (status, message_id, error, ticker))
        self.db.commit()


def deliver(send, cfg, text, attempts=3, sleep=time.sleep):
    last = None
    for i in range(attempts):
        try:
            _status, body = send("telegram", cfg, text)
            if body.get("ok") and (body.get("result") or {}).get("message_id"):
                return "SENT", body["result"]["message_id"], None
            last = "telegram answered without ok/message_id"
        except Exception as exc:                       # never log credentials: type and provider reason only
            last = f"{type(exc).__name__}: {getattr(exc, 'reason', '')}"[:200]
        sleep(2 * (i + 1))
    return "FAILED", None, last


def step(ledger, send, cfg, state, challenger, now, log=print):
    kind, d = opening(state, challenger, now)
    if kind in ("SKIP", "WAITING"):
        return kind
    text = message(kind, d)
    if not ledger.claim(kind, d, text, now):
        return "DUPLICATE"
    status, mid, err = deliver(send, cfg, text)
    ledger.result(d["ticker"], status, mid, err)
    log(json.dumps({"at": now.isoformat(), "ticker": d["ticker"], "kind": kind, "status": status,
                    "message_id": mid, "error": err}))
    return status


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    sys.path.insert(0, a.release)
    from src.signals_display import phone_alerts
    cfg = phone_alerts.load_config()
    if "telegram" not in phone_alerts.channels(cfg):
        print("telegram not configured; nothing sent"); return 2
    ledger, end = Ledger(a.db), datetime.now(timezone.utc) + timedelta(hours=a.hours)
    print(json.dumps({"started": datetime.now(timezone.utc).isoformat(), "ends": end.isoformat()}), flush=True)
    while datetime.now(timezone.utc) < end:
        try:
            step(ledger, phone_alerts.send, cfg, fetch_json("/api/state"), fetch_json("/api/challenger-opening"),
                 datetime.now(timezone.utc), log=lambda s: print(s, flush=True))
        except Exception as exc:
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "error": type(exc).__name__}), flush=True)
        time.sleep(a.poll)
    print(json.dumps({"ended": datetime.now(timezone.utc).isoformat()}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

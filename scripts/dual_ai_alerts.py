"""Telegram alert with the frozen opening calls of LIVE AI, CHALLENGER AI (v4) and their EXPERIMENTAL CONSENSUS,
one message per market opening (supervised service: deploy/oracle/kalshi-dual-ai-alerts.service).

Runs beside the server (never inside it) and reads only the read-only HTTP API on 127.0.0.1:8768, so it holds
no journal file open and cannot change the live call, the existing alerts, the model, records or orders.
It reuses the existing Telegram bot and chat via phone_alerts.load_config / send (no new credentials).

Rules:
  * a message is sent only for a genuine frozen opening: the challenger endpoint and /api/state must name the
    SAME ticker, the challenger's reproduced live P(UP) must equal the state's live P(UP), the record must be
    generated within GENERATED_MAX_S of the open, and the market must still have > OUTCOME_GUARD_S to run;
  * if the challenger is unavailable for a genuine live call, the message says so (nothing is substituted);
  * one message per ticker: claimed in a local SQLite table (PRIMARY KEY ticker) before sending;
  * the consensus is computed once from the two frozen calls and stored immutably (append-only triggers);
  * --hours 0 runs until stopped (the service); otherwise the run ends by itself after --hours.

  python scripts/dual_ai_alerts.py --release /home/opc/kalshi-preview-repair-<sha7> --db ~/dual-ai-alerts/sent.sqlite3
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

BASE = "http://127.0.0.1:8768"
THRESHOLD = 0.55
GENERATED_MAX_S = 150          # opening record must be made within 150 s of the market open
CHALLENGER_WAIT_S = 90         # how long after the open to wait for the challenger before reporting it unavailable
OUTCOME_GUARD_S = 60           # never send in the last minute of a market
LABEL = "CHALLENGER and COMBINED are EXPERIMENTAL and NOT VALIDATED (research only). LIVE AI is the official call."
COMBINED_LABEL = "EXPERIMENTAL CONSENSUS - NOT VALIDATED (average of two AIs, not a calibrated probability or a third model)"


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def direction(p):
    if not isinstance(p, (int, float)):
        return None
    return "UP" if p >= THRESHOLD else "DOWN" if p <= 1 - THRESHOLD else "WAIT"


def combine(live_up, challenger_up):
    """(consensus P(UP), direction). UP/DOWN only when both AIs independently call it and the average agrees."""
    c = (live_up + challenger_up) / 2
    if live_up >= THRESHOLD and challenger_up >= THRESHOLD and c >= THRESHOLD:
        return c, "UP"
    if live_up <= 1 - THRESHOLD and challenger_up <= 1 - THRESHOLD and c <= 1 - THRESHOLD:
        return c, "DOWN"
    return c, "WAIT"


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
        cp, cd = combine(float(live), float(ch["up"]))
        return "READY", {**d, "challenger_up": float(ch["up"]), "challenger": direction(ch["up"]),
                         "combined_up": cp, "combined": cd}
    if now - opens < timedelta(seconds=CHALLENGER_WAIT_S):
        return "WAITING", d
    reason = ch.get("reason") or ("challenger is for a different market" if ch.get("ticker") != ticker
                                  else f"challenger status {ch.get('status')}")
    return "CHALLENGER_UNAVAILABLE", {**d, "challenger_reason": str(reason)[:120]}


def message(kind, d):
    """Same four-line layout as the dashboard (src/signals_display/four_lines.py); minute-6 comes later."""
    from src.signals_display import four_lines
    ready = kind == "READY"
    body = four_lines.lines(live=(d["live"], d["live_up"]),
                            challenger=(d["challenger"], d["challenger_up"]) if ready else (None, None),
                            combined=(d["combined"], d["combined_up"]) if ready else (None, None))
    head = [f"{d['ticker']} · locked at open · expires {d['closes'].strftime('%H:%M UTC')}"]
    return "\n".join(head + body + [four_lines.FOOTER])


class Ledger:
    """openings: one immutable row per ticker (the frozen calls and consensus); deliveries: append-only log."""

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS openings(ticker TEXT PRIMARY KEY, opens_at TEXT, closes_at TEXT,
                generated_at TEXT, claimed_at TEXT, kind TEXT, live TEXT, live_up REAL, challenger TEXT,
                challenger_up REAL, combined TEXT, combined_up REAL, text TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY, ticker TEXT, at TEXT, status TEXT,
                message_id INTEGER, error TEXT);
            CREATE TRIGGER IF NOT EXISTS openings_immutable BEFORE UPDATE ON openings
                BEGIN SELECT RAISE(ABORT, 'opening calls are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS openings_no_delete BEFORE DELETE ON openings
                BEGIN SELECT RAISE(ABORT, 'opening calls are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS deliveries_append_only BEFORE UPDATE ON deliveries
                BEGIN SELECT RAISE(ABORT, 'delivery log is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS deliveries_no_delete BEFORE DELETE ON deliveries
                BEGIN SELECT RAISE(ABORT, 'delivery log is append-only'); END;""")
        self.db.commit()

    def _legacy(self, ticker):            # the time-boxed run's table: never resend a market it already sent
        try:
            return self.db.execute("SELECT 1 FROM sent WHERE ticker=?", (ticker,)).fetchone() is not None
        except sqlite3.OperationalError:
            return False

    def claim(self, kind, d, text, now):
        if self._legacy(d["ticker"]):
            return False
        cur = self.db.execute("INSERT OR IGNORE INTO openings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (d["ticker"], d["opens"].isoformat(), d["closes"].isoformat(), d["generated"].isoformat(),
                               now.isoformat(), kind, d["live"], d["live_up"], d.get("challenger"),
                               d.get("challenger_up"), d.get("combined"), d.get("combined_up"), text))
        self.db.commit()
        return cur.rowcount == 1

    def result(self, ticker, status, message_id=None, error=None, now=None):
        self.db.execute("INSERT INTO deliveries(ticker, at, status, message_id, error) VALUES (?,?,?,?,?)",
                        (ticker, (now or datetime.now(timezone.utc)).isoformat(), status, message_id, error))
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
    try:                                       # model version per opening, so results can be split by version
        model = (((state or {}).get("prediction_engine") or {}).get("initial") or {}).get("model")
        ledger.db.execute("CREATE TABLE IF NOT EXISTS opening_models(ticker TEXT PRIMARY KEY, model TEXT, recorded_at TEXT)")
        ledger.db.execute("INSERT OR IGNORE INTO opening_models VALUES (?,?,?)",
                          (d["ticker"], json.dumps(model, default=str)[:300], now.isoformat()))
        ledger.db.commit()
    except Exception:
        pass
    status, mid, err = deliver(send, cfg, text)
    ledger.result(d["ticker"], status, mid, err)
    log(json.dumps({"at": now.isoformat(), "ticker": d["ticker"], "kind": kind, "status": status,
                    "message_id": mid, "error": err}))
    return status


def market_history(pages=3, fetch=None):
    """Recent settled markets from the read-only API, following its cursor (3 pages ~ 15 h, enough to ride out
    a service restart)."""
    from urllib.parse import urlencode
    fetch = fetch or fetch_json
    markets, query = [], ""
    for _ in range(pages):
        try:
            d = fetch("/api/market-history" + query)
        except Exception:
            break
        markets += d.get("markets") or []
        p = d.get("pagination") or {}
        if not p.get("has_more") or not p.get("next_before_close"):
            break
        query = "?" + urlencode({"before_close": p["next_before_close"], "before_ticker": p.get("next_before_ticker") or ""})
    return {"markets": markets}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--hours", type=float, default=0.0, help="0 = run until stopped")
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args(argv)
    sys.path.insert(0, a.release)
    from src.signals_display import phone_alerts
    cfg = phone_alerts.load_config()
    if "telegram" not in phone_alerts.channels(cfg):
        print("telegram not configured; nothing sent"); return 2
    ledger = Ledger(a.db)
    runner = None
    try:                                           # minute-6 research rides in the same loop (one service)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from minute6_runner import Minute6Runner, server_candles
        runner = Minute6Runner(a.db, str(Path(a.db).with_name("minute6_latest.json")), phone_alerts.send, cfg,
                               candles=lambda: server_candles(fetch_json),
                               history=market_history, log=lambda s: print(s, flush=True))
    except Exception as exc:
        print(json.dumps({"minute6": "disabled", "error": type(exc).__name__}), flush=True)
    end =datetime.max.replace(tzinfo=timezone.utc) if a.hours <= 0 else datetime.now(timezone.utc) + timedelta(hours=a.hours)
    print(json.dumps({"started": datetime.now(timezone.utc).isoformat(), "ends": end.isoformat()}), flush=True)
    while datetime.now(timezone.utc) < end:
        state = None
        try:
            state = fetch_json("/api/state")
            step(ledger, phone_alerts.send, cfg, state, fetch_json("/api/challenger-opening"),
                 datetime.now(timezone.utc), log=lambda s: print(s, flush=True))
        except Exception as exc:
            print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "error": type(exc).__name__}), flush=True)
        if runner is not None and state is not None:
            try:
                runner.step(state)
            except Exception as exc:
                print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "minute6_error": type(exc).__name__}), flush=True)
        time.sleep(a.poll)
    print(json.dumps({"ended": datetime.now(timezone.utc).isoformat()}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

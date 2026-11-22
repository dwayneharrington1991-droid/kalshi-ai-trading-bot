"""Minute-6 research runner, driven by the three-AI alert service loop (same process, no extra service).

* Locks ONE minute-6 reading per market between opens+6:05 and opens+7:00 from information available at the
  cutoff (Coinbase candles up to the one closing at +6:00, Kalshi threshold and engine P(UP) for the SAME
  ticker from /api/state); stores it immutably (append-only triggers) and sends one Telegram research alert.
* Every poll it writes minute6_latest.json (live analytics + this market's locked reading + scoreboard) that
  the dashboard reads through /api/minute6. The scoreboard uses official settlements from /api/market-history.
Never trades, never touches the opening calls or their scoreboard.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from src.signals_display import minute6 as m6

CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60"
FEE_RATE, SLIPPAGE = 0.07, 0.01


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None


def fetch_candles():                   # public Coinbase endpoint: ~2.5 min behind, so only a fallback
    req = Request(CANDLES_URL, headers={"User-Agent": "kalshi-signals-research/1.0"})
    with urlopen(req, timeout=10) as r:
        return json.loads(r.read())       # [time, low, high, open, close, volume], newest first


def merge_bars(minute_bars, bars_15s):
    """One-minute candles in Coinbase layout from the server's own Coinbase feeds: /api/signal-chart minute
    bars (history) with each minute's close taken from the final 15-second trade bar that starts at minute+45
    (the last trade of that minute, available ~7 s after the minute ends). Only complete minutes are returned."""
    by = {}
    for b in minute_bars or []:
        t = int(b["start"])
        by[t] = [t, float(b["low"]), float(b["high"]), float(b["open"]), float(b["close"]), float(b.get("volume") or 0)]
    groups = {}
    for b in bars_15s or []:
        groups.setdefault(int(b["start"]) // 60 * 60, []).append(b)
    for t, bs in groups.items():
        bs.sort(key=lambda b: b["start"])
        if int(bs[-1]["start"]) != t + 45:
            continue                       # minute not finished (its last 15 s bar is not final yet)
        lo, hi = min(float(b["low"]) for b in bs), max(float(b["high"]) for b in bs)
        vol = sum(float(b.get("volume") or 0) for b in bs)
        if t in by:
            by[t][4] = float(bs[-1]["close"])
        elif len(bs) == 4:
            by[t] = [t, lo, hi, float(bs[0]["open"]), float(bs[-1]["close"]), vol]
    return sorted(by.values(), key=lambda c: -c[0])


def server_candles(fetch):
    return merge_bars(fetch("/api/signal-chart").get("bars"), fetch("/api/live-chart").get("bars"))


def wilson(k, n, z=1.96):
    if not n:
        return [None, None]
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [c - h, c + h]


class Minute6Runner:
    def __init__(self, db_path, out_path, send, cfg, candles=fetch_candles, history=None, log=print):
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS minute6(ticker TEXT PRIMARY KEY, opens_at TEXT, closes_at TEXT, locked_at TEXT,
                cutoff TEXT, threshold REAL, price REAL, direction TEXT, components_agree INTEGER, reason TEXT,
                a_p_up REAL, a_dir TEXT, b_score REAL, b_dir TEXT, c_p_up REAL, c_dir TEXT,
                up_ask REAL, down_ask REAL, up_bid REAL, down_bid REAL, payload TEXT);
            CREATE TABLE IF NOT EXISTS minute6_deliveries(id INTEGER PRIMARY KEY, ticker TEXT, at TEXT, status TEXT,
                message_id INTEGER, error TEXT);
            CREATE TRIGGER IF NOT EXISTS minute6_immutable BEFORE UPDATE ON minute6
                BEGIN SELECT RAISE(ABORT, 'minute-6 readings are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS minute6_no_delete BEFORE DELETE ON minute6
                BEGIN SELECT RAISE(ABORT, 'minute-6 readings are immutable'); END;
            CREATE TABLE IF NOT EXISTS minute6_outcomes(ticker TEXT PRIMARY KEY, outcome TEXT, seen_at TEXT);
            CREATE TRIGGER IF NOT EXISTS minute6_outcomes_immutable BEFORE UPDATE ON minute6_outcomes
                BEGIN SELECT RAISE(ABORT, 'outcomes are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS minute6_deliveries_append BEFORE UPDATE ON minute6_deliveries
                BEGIN SELECT RAISE(ABORT, 'append-only'); END;""")
        self.db.commit()
        self.out_path, self.send, self.cfg, self.candles, self.history, self.log = out_path, send, cfg, candles, history, log
        self.cache = (0.0, None)
        self.board = (0.0, None)

    def _candles(self, now_mono):
        if now_mono - self.cache[0] > 12 or self.cache[1] is None:
            self.cache = (now_mono, self.candles())
        return self.cache[1]

    @staticmethod
    def _market(state):
        pe = (state or {}).get("prediction_engine") or {}
        ticker, closes = pe.get("ticker"), _ts(pe.get("closes_at"))
        if not ticker or not closes:
            return None
        return {"ticker": ticker, "opens": closes - timedelta(minutes=15), "closes": closes,
                "threshold": pe.get("threshold"), "engine_up": pe.get("up"), "engine_at": _ts(pe.get("updated_at"))}

    @staticmethod
    def _quotes(state, ticker):
        sm = (state or {}).get("scalping_monitor") or {}
        if sm.get("ticker") != ticker:
            return {}
        s = sm.get("sides") or {}
        return {"up_ask": (s.get("up") or {}).get("ask"), "up_bid": (s.get("up") or {}).get("bid"),
                "down_ask": (s.get("down") or {}).get("ask"), "down_bid": (s.get("down") or {}).get("bid")}

    def maybe_lock(self, state, now, now_mono):
        m = self._market(state)
        if not m:
            return "NO MARKET"
        since = (now - m["opens"]).total_seconds()
        if not m6.DECISION_AT_S + 5 <= since <= m6.DECISION_LATEST_S:
            return "OUTSIDE WINDOW"
        if self.db.execute("SELECT 1 FROM minute6 WHERE ticker=?", (m["ticker"],)).fetchone():
            return "ALREADY LOCKED"
        thr = m["threshold"] if isinstance(m["threshold"], (int, float)) and m["threshold"] > 0 else None
        try:                                   # fresh fetch: the candle closing at +6:00 is published a few seconds late
            self.cache = (now_mono, self.candles())
        except Exception:
            pass
        closes = m6.candles_to_closes(self.cache[1] or [], int(m["opens"].timestamp()))
        if closes is None and since < m6.DECISION_LATEST_S - 10:
            return "WAITING FOR CANDLE"        # retry next poll; lock without it only at the deadline
        a = m6.component_a(closes, thr) if closes else {"status": "INELIGIBLE", "reason": "one-minute candles missing"}
        b = m6.component_b(closes) if closes else {"status": "INELIGIBLE", "reason": "one-minute candles missing"}
        eng_ok = m["engine_at"] is not None and m["engine_at"] <= now
        c = m6.component_c(m["engine_up"] if eng_ok else None)
        r = m6.consensus(a, b, c)
        q = self._quotes(state, m["ticker"])
        cutoff = m["opens"] + timedelta(seconds=m6.DECISION_AT_S)
        row = (m["ticker"], m["opens"].isoformat(), m["closes"].isoformat(), now.isoformat(), cutoff.isoformat(), thr,
               closes[-1] if closes else None, r["direction"], int(r["components_agree"]), r["reason"],
               a.get("p_up"), a.get("direction"), b.get("score"), b.get("direction"), c.get("p_up"), c.get("direction"),
               q.get("up_ask"), q.get("down_ask"), q.get("up_bid"), q.get("down_bid"), json.dumps(r, default=str))
        cur = self.db.execute("INSERT OR IGNORE INTO minute6 VALUES (" + ",".join("?" * 21) + ")", row)
        self.db.commit()
        if cur.rowcount != 1:
            return "ALREADY LOCKED"
        if not self.valid(r):                  # recorded, but no phone update for an invalid reading
            self.log(json.dumps({"at": now.isoformat(), "minute6": m["ticker"], "direction": r["direction"],
                                 "status": "NOT SENT (fewer than two eligible components)"}))
            return "RECORDED"
        text = self.message(m, r, q, now)
        status, mid, err = "FAILED", None, None
        for i in range(3):
            try:
                _s, body = self.send("telegram", self.cfg, text)
                if body.get("ok") and (body.get("result") or {}).get("message_id"):
                    status, mid = "SENT", body["result"]["message_id"]
                    break
                err = "telegram answered without ok/message_id"
            except Exception as exc:
                err = f"{type(exc).__name__}: {getattr(exc, 'reason', '')}"[:200]
            time.sleep(2 * (i + 1))
        self.db.execute("INSERT INTO minute6_deliveries(ticker, at, status, message_id, error) VALUES (?,?,?,?,?)",
                        (m["ticker"], datetime.now(timezone.utc).isoformat(), status, mid, err))
        self.db.commit()
        self.log(json.dumps({"at": now.isoformat(), "minute6": m["ticker"], "direction": r["direction"],
                             "status": status, "message_id": mid, "error": err}))
        return status

    @staticmethod
    def valid(r):
        return r.get("eligible", 0) >= 2 and r.get("direction") in ("UP", "DOWN", "WAIT")

    def message(self, m, r, q, now):
        """Same four lines as the dashboard: the three locked opening calls of THIS market, then minute-6."""
        from src.signals_display import four_lines
        o = self.db.execute("SELECT live, live_up, challenger, challenger_up, combined, combined_up FROM openings "
                            "WHERE ticker=?", (m["ticker"],)).fetchone() if self._has_openings() else None
        o = o or (None,) * 6
        body = four_lines.lines((o[0], o[1]), (o[2], o[3]), (o[4], o[5]),
                                minute6=(r["direction"], r["consensus_p_up_average"], q))
        head = [f"{m['ticker']} · minute-6 update · expires {m['closes'].strftime('%H:%M UTC')}"]
        return "\n".join(head + body + [four_lines.FOOTER])

    def _has_openings(self):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name='openings'").fetchone() is not None

    def scoreboard(self, now_mono):
        if self.history is None:
            return None
        if now_mono - self.board[0] < 120 and self.board[1] is not None:
            return self.board[1]
        try:                                   # keep official settlements locally: the API pages only recent markets
            for x in (self.history() or {}).get("markets", []):
                if x.get("outcome_status") == "VERIFIED_OFFICIAL" and x.get("final_outcome") in ("UP", "DOWN"):
                    self.db.execute("INSERT OR IGNORE INTO minute6_outcomes VALUES (?,?,?)",
                                    (x["ticker"], x["final_outcome"], datetime.now(timezone.utc).isoformat()))
            self.db.commit()
        except Exception:
            pass
        out = dict(self.db.execute("SELECT ticker, outcome FROM minute6_outcomes"))
        rows = self.db.execute("SELECT ticker, direction, price, threshold, up_ask, down_ask FROM minute6").fetchall()
        settled = [(t, d, p, th, ua, da, out[t]) for t, d, p, th, ua, da in rows if t in out]
        y = lambda v: 1 if v == "UP" else 0
        calls = [s for s in settled if s[1] in ("UP", "DOWN")]
        k = sum((s[1] == "UP") == (y(s[6]) == 1) for s in calls)
        base = [s for s in settled if s[2] and s[3]]
        kb = sum((s[2] > s[3]) == (y(s[6]) == 1) for s in base)
        evs = []
        for s in calls:
            price = s[4] if s[1] == "UP" else s[5]
            if isinstance(price, (int, float)) and 0 < price < 1:
                win = (s[1] == "UP") == (y(s[6]) == 1)
                evs.append((1 if win else 0) - price - FEE_RATE * price * (1 - price) - SLIPPAGE)
        board = {"locked": len(rows), "settled": len(settled), "up": sum(s[1] == "UP" for s in settled),
                 "down": sum(s[1] == "DOWN" for s in settled), "wait": sum(s[1] == "WAIT" for s in settled),
                 "calls": len(calls), "correct": k, "accuracy": k / len(calls) if calls else None,
                 "accuracy_ci95": wilson(k, len(calls)), "coverage": len(calls) / len(settled) if settled else None,
                 "price_vs_threshold_baseline": {"n": len(base), "correct": kb},
                 "est_net_per_contract_at_lock_ask": (sum(evs) / len(evs)) if evs else None, "priced_calls": len(evs),
                 "scope": "Prospective minute-6 research ledger from deployment; official Kalshi settlements; separate from minute-0"}
        self.board = (now_mono, board)
        return board

    def write_latest(self, state, now, now_mono):
        m = self._market(state)
        out = {"label": m6.LABEL, "generated_at": now.isoformat(), "status": "WAITING"}
        if m:
            out.update(ticker=m["ticker"], opens_at=m["opens"].isoformat(), closes_at=m["closes"].isoformat(),
                       lock_at=(m["opens"] + timedelta(seconds=m6.DECISION_AT_S)).isoformat())
            try:
                cs = [c for c in self._candles(now_mono) if int(c[0]) + 60 <= now.timestamp()]   # complete minutes only
                thr = m["threshold"] if isinstance(m["threshold"], (int, float)) else None
                out["analytics"] = m6.analytics(cs, thr, now.timestamp(), m["closes"].timestamp())
            except Exception as exc:
                out["analytics"] = {"status": "UNAVAILABLE", "reason": type(exc).__name__}
            row = self.db.execute("SELECT locked_at, direction, components_agree, reason, payload FROM minute6 WHERE ticker=?",
                                  (m["ticker"],)).fetchone()
            if row:
                reading = json.loads(row[4])
                out.update(status="LOCKED", locked_at=row[0], direction=row[1], components_agree=bool(row[2]),
                           reason=row[3], reading=reading, valid=self.valid(reading))
                from src.signals_display import four_lines
                if out["valid"]:
                    q = dict(zip(("up_ask", "down_ask"), self.db.execute(
                        "SELECT up_ask, down_ask FROM minute6 WHERE ticker=?", (m["ticker"],)).fetchone()))
                    out["line"] = four_lines.minute6_line(row[1], reading.get("consensus_p_up_average"), q)
                else:
                    out["line"] = four_lines.UNAVAILABLE
            else:
                from src.signals_display import four_lines
                pending = now < m["opens"] + timedelta(seconds=m6.DECISION_LATEST_S)
                out["status"] = "WAITING FOR MINUTE 6" if pending \
                    else "NO READING (service was not running at minute 6; nothing substituted)"
                out["line"] = four_lines.PENDING if pending else four_lines.UNAVAILABLE
        out["scoreboard"] = self.scoreboard(now_mono)
        tmp = self.out_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f, default=str)
        os.replace(tmp, self.out_path)
        return out

    def step(self, state, now=None, now_mono=None):
        now = now or datetime.now(timezone.utc)
        now_mono = time.monotonic() if now_mono is None else now_mono
        status = self.maybe_lock(state, now, now_mono)
        self.write_latest(state, now, now_mono)
        return status

"""User-reported journal + exit guidance ONLY. No broker/account/trading client."""
from .storage import connect as storage_connect
from datetime import timedelta
import json
import math
import sqlite3

from .model import number, timestamp


class ManualJournal:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS manual_entries (
                id INTEGER PRIMARY KEY,ticker TEXT,side TEXT,price REAL,quantity REAL,fees REAL,entered_at TEXT,
                target REAL,stop REAL,max_hold INTEGER,slippage REAL,status TEXT DEFAULT 'OPEN',
                last_guidance TEXT,last_event_id INTEGER,created_at TEXT);
                CREATE TABLE IF NOT EXISTS manual_exits (id INTEGER PRIMARY KEY,entry_id INTEGER,
                price REAL,quantity REAL,fees REAL,exited_at TEXT,recorded_at TEXT);
                CREATE TABLE IF NOT EXISTS manual_alerts (id INTEGER PRIMARY KEY,entry_id INTEGER,
                guidance TEXT,observed_at TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS manual_entries_status_id ON manual_entries(status,id DESC);
                CREATE INDEX IF NOT EXISTS manual_exits_entry_id ON manual_exits(entry_id,id);
            """
            )

    def connect(self):
        return storage_connect(self.path)

    def enter(self, data, now, defaults):
        allowed = {
            "ticker",
            "side",
            "price",
            "quantity",
            "fees",
            "entered_at",
            "target",
            "stop",
            "max_hold",
        }
        if set(data) - allowed:
            raise ValueError("Unknown entry fields")
        ticker = data["ticker"]
        side = data["side"]
        if (
            not isinstance(ticker, str)
            or not ticker.startswith("KXBTC15M-")
            or len(ticker) > 120
            or any(
                c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for c in ticker
            )
        ):
            raise ValueError("A KXBTC15M contract ticker is required")
        if side not in ("up", "down"):
            raise ValueError("Side must be up or down")
        price = number(data["price"], 0, 1)
        quantity = number(data["quantity"], 0.01, 1e6)
        fees = number(data["fees"], 0, 1e6)
        at = timestamp(data["entered_at"])
        if at > now + timedelta(seconds=5):
            raise ValueError("Entry time cannot be in the future")
        target = number(data.get("target", defaults["target"]), 0.000001, 1)
        stop = number(data.get("stop", defaults["stop"]), 0.000001, 1)
        hold = data.get("max_hold", defaults["horizon"])
        if isinstance(hold, bool) or not isinstance(hold, int) or not 1 <= hold <= 86400:
            raise ValueError("Holding time must be 1..86400 seconds")
        with self.connect() as db:
            if (
                db.execute("SELECT COUNT(*) FROM manual_entries WHERE status='OPEN'").fetchone()[0]
                >= 20
            ):
                raise ValueError("Maximum 20 open manual records; record actual exits first")
            cur = db.execute(
                """INSERT INTO manual_entries
                (ticker,side,price,quantity,fees,entered_at,target,stop,max_hold,slippage,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ticker,
                    side,
                    price,
                    quantity,
                    fees,
                    at.isoformat(),
                    target,
                    stop,
                    hold,
                    defaults["slippage"],
                    now.isoformat(),
                ),
            )
            return cur.lastrowid

    def exit(self, data, now):
        if set(data) - {"entry_id", "price", "quantity", "fees", "exited_at"}:
            raise ValueError("Unknown exit fields")
        price = number(data["price"], 0, 1)
        qty = number(data["quantity"], 0.01, 1e6)
        fees = number(data["fees"], 0, 1e6)
        at = timestamp(data["exited_at"])
        if at > now + timedelta(seconds=5):
            raise ValueError("Exit time cannot be in the future")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM manual_entries WHERE id=?", (data["entry_id"],)
            ).fetchone()
            if row is None:
                raise ValueError("Entry record not found")
            if at < timestamp(row["entered_at"]):
                raise ValueError("Exit precedes entry")
            used = db.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM manual_exits WHERE entry_id=?", (row["id"],)
            ).fetchone()[0]
            if qty > row["quantity"] - used + 1e-9:
                raise ValueError("Exit quantity exceeds remaining recorded quantity")
            db.execute(
                "INSERT INTO manual_exits (entry_id,price,quantity,fees,exited_at,recorded_at) VALUES (?,?,?,?,?,?)",
                (row["id"], price, qty, fees, at.isoformat(), now.isoformat()),
            )
            if abs(row["quantity"] - used - qty) < 1e-9:
                db.execute("UPDATE manual_entries SET status='CLOSED' WHERE id=?", (row["id"],))

    def records(self, *, open_only=False, before=None, before_id=None, limit=None):
        if before_id is not None:
            if before is not None and before != before_id:
                raise ValueError("Conflicting history cursors")
            before = before_id
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("History cursor must be a positive integer")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
            raise ValueError("History page size must be 1..100")
        clauses, params = [], []
        if open_only:
            clauses.append("status='OPEN'")
        if before is not None:
            clauses.append("id<?")
            params.append(before)
        query = "SELECT * FROM manual_entries"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit + 1)
        with storage_connect(self.path, readonly=True) as db:
            entries = [dict(r) for r in db.execute(query, params)]
            has_more = limit is not None and len(entries) > limit
            if limit is not None:
                entries = entries[:limit]
            ids = [entry["id"] for entry in entries]
            exits = (
                [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM manual_exits WHERE entry_id IN ("
                        + ",".join("?" for _ in ids)
                        + ") ORDER BY id",
                        ids,
                    )
                ]
                if ids
                else []
            )
        grouped = {}
        for fill in exits:
            grouped.setdefault(fill["entry_id"], []).append(fill)
        for entry in entries:
            fills = grouped.get(entry["id"], [])
            used = sum(r["quantity"] for r in fills)
            entry["remaining_quantity"] = max(0, entry["quantity"] - used)
            entry["reported_realized_net_pnl"] = (
                sum(r["price"] * r["quantity"] - r["fees"] for r in fills)
                - entry["price"] * used
                - entry["fees"] * used / entry["quantity"]
            )
            entry["user_reported"] = True
            entry["broker_verified"] = False
        return {
            "entries": entries,
            "exits": exits,
            "has_more": has_more,
            "next_before_id": entries[-1]["id"] if has_more else None,
            "notice": "User-reported records, never broker-verified. No displayed signal or alert is a fill.",
        }

    def guidance(self, quotes, now, current_state):
        result = []
        for entry in self.records(open_only=True)["entries"]:
            if entry["status"] != "OPEN":
                continue
            quote = quotes.get(entry["ticker"])
            state = "DATA STALE — CHECK POSITION"
            reason = "Waiting for fresh post-start quotes for this recorded contract."
            bid = None
            net = None
            fee_total = None
            try:
                if not quote:
                    raise ValueError(reason)
                if not 0 <= (now - timestamp(quote["observed_at"])).total_seconds() <= 5:
                    raise ValueError("Quotes are older than five seconds; check manually.")
                if quote.get("book_diagnostic"):
                    raise ValueError("Executable depth is unavailable; check manually.")
                q = quote["quotes"][entry["side"]]
                if q.get("bid_size", 0) < entry["remaining_quantity"]:
                    raise ValueError(
                        "Insufficient current bid depth for the remaining recorded quantity."
                    )
                if quote["fee_type"] not in ("quadratic", "quadratic_with_maker_fees"):
                    raise ValueError("Exit fee schedule is unverified.")
                bid = number(q["bid"], 0, 1)
                mult = number(quote["fee_multiplier"], 0, 100)
                exit_price = max(0, bid - entry["slippage"])
                qty = entry["remaining_quantity"]
                fee_total = (
                    math.ceil(0.07 * mult * qty * exit_price * (1 - exit_price) * 100 - 1e-12) / 100
                )
                net = (
                    qty * (exit_price - entry["price"])
                    - entry["fees"] * qty / entry["quantity"]
                    - fee_total
                )
                unit = net / qty
                state = "HOLD"
                reason = "No recorded net target, stop, time limit or supported invalidation condition reached."
                if now >= timestamp(entry["entered_at"]) + timedelta(seconds=entry["max_hold"]):
                    state = "EXIT — TIME LIMIT"
                    reason = "Recorded maximum holding time reached."
                elif unit >= entry["target"]:
                    state = "EXIT — TARGET"
                    reason = "Executable bid less assumed slippage and fees reaches the recorded net target."
                elif unit <= -entry["stop"]:
                    state = "EXIT — STOP"
                    reason = "Executable bid less assumed slippage and fees reaches the recorded net stop."
                elif (current_state.get("market") or {}).get("ticker") == entry[
                    "ticker"
                ] and not current_state.get("stale", True):
                    e = current_state.get("estimate", {})
                    sensitivity = current_state.get("model", {}).get(
                        "settlement_probability_sensitivity"
                    )
                    opposed = (
                        entry["side"] == "up"
                        and current_state.get("direction") == "DOWN"
                        and (e.get("settlement_down") or 0) >= 0.8
                        and sensitivity
                        and sensitivity[1] <= 0.2
                    ) or (
                        entry["side"] == "down"
                        and current_state.get("direction") == "UP"
                        and (e.get("settlement_up") or 0) >= 0.8
                        and sensitivity
                        and sensitivity[0] >= 0.8
                    )
                    if opposed:
                        state = "EXIT — SETUP INVALIDATED"
                        reason = "Fresh same-contract experimental forecast strongly opposes the recorded side (80% sensitivity gate). WAIT alone does not invalidate an entry."
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                reason = str(exc)
            event_id = entry["last_event_id"]
            event_time = None
            # Transient startup staleness never erases the last genuine guidance event.
            if state != "DATA STALE — CHECK POSITION":
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    latest = db.execute(
                        "SELECT last_guidance,last_event_id FROM manual_entries WHERE id=?",
                        (entry["id"],),
                    ).fetchone()
                    event_id = latest["last_event_id"]
                    if state != latest["last_guidance"]:
                        cur = db.execute(
                            "INSERT INTO manual_alerts (entry_id,guidance,observed_at,payload) VALUES (?,?,?,?)",
                            (
                                entry["id"],
                                state,
                                now.isoformat(),
                                json.dumps({"bid": bid, "estimated_net_pnl": net}),
                            ),
                        )
                        event_id = cur.lastrowid
                        db.execute(
                            "UPDATE manual_entries SET last_guidance=?,last_event_id=? WHERE id=?",
                            (state, event_id, entry["id"]),
                        )
                    if event_id:
                        row = db.execute(
                            "SELECT observed_at FROM manual_alerts WHERE id=?", (event_id,)
                        ).fetchone()
                        event_time = row[0] if row else None
            result.append(
                {
                    **entry,
                    "guidance": state,
                    "reason": reason,
                    "current_bid": bid,
                    "estimated_net_pnl": net,
                    "estimated_exit_fee": fee_total,
                    "quote_timestamp": quote.get("observed_at") if quote else None,
                    "alert_timestamp": event_time or now.isoformat(),
                    "alert_id": event_id
                    if state != "DATA STALE — CHECK POSITION"
                    else f'stale-{entry["id"]}',
                    "notice": "Guidance only, no guaranteed fill or price. Record the actual exit yourself; this record remains OPEN.",
                }
            )
        return result

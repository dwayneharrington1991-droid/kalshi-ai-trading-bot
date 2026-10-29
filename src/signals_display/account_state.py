"""Confirmed account snapshots, explicit guidance opt-in, no execution capability."""
from .storage import connect as storage_connect
from datetime import timedelta
import json
import math
import sqlite3
import hashlib
import threading
from copy import deepcopy

from .account_readonly import (
    AccountReadError,
    decimal,
    normalized_fill,
    fifo_basis,
    confirmed_cycles,
)
from .model import timestamp, number

REQUIRED_LIMITS = (
    "max_add_quantity",
    "max_per_contract_quantity",
    "max_account_quantity",
    "max_add_cost_dollars",
    "minimum_cash_reserve",
)


class AccountJournal:
    def __init__(self, path):
        self.path = str(path)
        # Reconciliation owns expensive FIFO history work; request threads copy
        # a bounded immutable view instead of rescanning every confirmed fill.
        self._view_lock = threading.RLock()
        self._snapshot_cache = None
        self._completed_cache = []
        self._cycles_signature = None
        self._cycles_cache = []
        with self.connect() as db:
            db.executescript(
                """CREATE TABLE IF NOT EXISTS monitor_account_fills (fill_id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS monitor_account_holdings (id INTEGER PRIMARY KEY,ticker TEXT,side TEXT,quantity REAL,
                    entry_price REAL,entry_fees REAL,entered_at TEXT,basis_verified INTEGER,basis_reason TEXT,
                    first_seen TEXT,updated_at TEXT,status TEXT,source TEXT DEFAULT 'account-confirmed existing/manual');
                CREATE TABLE IF NOT EXISTS monitor_account_choices (holding_id INTEGER PRIMARY KEY,enabled INTEGER);
                CREATE TABLE IF NOT EXISTS monitor_account_state (id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT);
                CREATE TABLE IF NOT EXISTS monitor_account_events (id INTEGER PRIMARY KEY,holding_id INTEGER,kind TEXT,observed_at TEXT,quantity REAL);
                CREATE TABLE IF NOT EXISTS monitor_account_guidance_events (id INTEGER PRIMARY KEY,holding_id INTEGER,guidance TEXT,quantity REAL,observed_at TEXT);
                CREATE TABLE IF NOT EXISTS monitor_account_settings (name TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS monitor_account_fill_audit (
                    fill_id TEXT,fingerprint TEXT,payload_kind TEXT,ticker TEXT,status TEXT,reason TEXT,
                    payload TEXT,observed_at TEXT,PRIMARY KEY(fill_id,fingerprint,payload_kind));
                CREATE TABLE IF NOT EXISTS monitor_account_fill_resolution (
                    fill_id TEXT PRIMARY KEY,ticker TEXT,order_id TEXT,status TEXT,reason TEXT,observed_at TEXT);
                CREATE TABLE IF NOT EXISTS monitor_account_completed_index (
                    id INTEGER PRIMARY KEY,cycle_id TEXT NOT NULL UNIQUE);
                CREATE INDEX IF NOT EXISTS monitor_account_holdings_status_id ON monitor_account_holdings(status,id DESC);
                CREATE INDEX IF NOT EXISTS monitor_account_resolution_status_ticker ON monitor_account_fill_resolution(status,ticker);
                CREATE INDEX IF NOT EXISTS monitor_account_guidance_holding_id ON monitor_account_guidance_events(holding_id,id DESC);
            """
            )

    def connect(self):
        return storage_connect(self.path)

    def bind(self, identity, *, allow_empty_rotation=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM monitor_account_settings WHERE name='credential_binding'"
            ).fetchone()
            if row and json.loads(row[0]) != identity:
                tables = (
                    "monitor_account_fills",
                    "monitor_account_holdings",
                    "monitor_account_state",
                    "monitor_account_events",
                    "monitor_account_guidance_events",
                    "monitor_account_choices",
                    "monitor_account_fill_audit",
                    "monitor_account_fill_resolution",
                )
                empty = not any(
                    db.execute("SELECT EXISTS(SELECT 1 FROM " + table + ")").fetchone()[0]
                    for table in tables
                )
                empty = (
                    empty
                    and not db.execute(
                        "SELECT EXISTS(SELECT 1 FROM monitor_account_settings WHERE name!='credential_binding')"
                    ).fetchone()[0]
                )
                if allow_empty_rotation and empty:
                    db.execute(
                        "UPDATE monitor_account_settings SET payload=? WHERE name='credential_binding'",
                        (json.dumps(identity),),
                    )
                    return
                raise AccountReadError(
                    "Monitoring key binding changed; use a separate account-store file to avoid mixing accounts. Existing records are preserved."
                )
            db.execute(
                "INSERT OR IGNORE INTO monitor_account_settings VALUES ('credential_binding',?)",
                (json.dumps(identity),),
            )

    def select(self, identifier, enabled):
        with self._view_lock:
            self._select(identifier, enabled)
            if self._snapshot_cache is not None:
                updated = deepcopy(self._snapshot_cache)
                for holding in updated.get("holdings", []):
                    if holding["id"] == identifier:
                        holding["selected"] = int(enabled)
                self._snapshot_cache = updated

    def _select(self, identifier, enabled):
        if not isinstance(identifier, int) or type(enabled) is not bool:
            raise ValueError("Invalid monitoring selection")
        with self.connect() as db:
            row = db.execute(
                "SELECT ticker,status FROM monitor_account_holdings WHERE id=?", (identifier,)
            ).fetchone()
            if row is None or row["status"] != "OPEN" or not row["ticker"].startswith("KXBTC15M-"):
                raise ValueError("Select an open KXBTC15M holding")
            db.execute(
                "INSERT OR REPLACE INTO monitor_account_choices VALUES (?,?)",
                (identifier, int(enabled)),
            )

    def limits(self, data=None):
        with self._view_lock:
            result = self._limits(data)
            if data is not None and self._snapshot_cache is not None:
                updated = deepcopy(self._snapshot_cache)
                updated["limits"] = deepcopy(result)
                self._snapshot_cache = updated
            return result

    def _limits(self, data=None):
        with storage_connect(self.path, readonly=data is None) as db:
            if data is not None:
                if set(data) != set(REQUIRED_LIMITS):
                    raise ValueError(
                        "All explicit exposure/cash limits are required; adding is otherwise disabled"
                    )
                cleaned = {name: number(data[name], 0, 1e9) for name in REQUIRED_LIMITS}
                if any(cleaned[n] <= 0 for n in REQUIRED_LIMITS if n != "minimum_cash_reserve"):
                    raise ValueError("Positive quantity/cost limits required")
                db.execute(
                    "INSERT OR REPLACE INTO monitor_account_settings VALUES ('limits',?)",
                    (json.dumps(cleaned),),
                )
                return cleaned
            row = db.execute(
                "SELECT payload FROM monitor_account_settings WHERE name='limits'"
            ).fetchone()
            return json.loads(row[0]) if row else None

    def reconcile(self, snapshot):
        with self._view_lock:
            fills = self._reconcile(snapshot)
            # This runs in the account worker. Keep the prior view readable
            # until its complete replacement has been built and committed.
            self._refresh_snapshot(fills)

    def _reconcile(self, snapshot):
        if snapshot.get("complete") is not True:
            raise AccountReadError("Incomplete snapshot cannot close/reconcile holdings")
        observed = snapshot["observed_at"]
        timestamp(observed)
        entries = []
        for raw in snapshot["fills"]:
            payload = json.dumps(raw, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode()).hexdigest()
            fields = raw if isinstance(raw, dict) else {}
            identifier = str(
                fields.get("fill_id") or fields.get("trade_id") or "unidentified:" + digest
            )
            ticker = fields.get("ticker") or fields.get("market_ticker") or ""
            ticker = ticker if isinstance(ticker, str) else ""
            item = dict(
                fill_id=identifier,
                ticker=ticker,
                order_id=str(fields.get("order_id") or ""),
                digest=digest,
                payload=payload,
                status="READY",
                reason=None,
                normalized=None,
            )
            try:
                fill = normalized_fill(raw)
                if not fill["ticker"]:
                    raise AccountReadError("Missing confirmed fill ticker")
                if timestamp(fill["at"]) > timestamp(observed) + timedelta(seconds=5):
                    raise AccountReadError("Future-dated fill; clock verification required")
                item["normalized"] = fill
                if fill.get("direction_warning"):
                    item.update(
                        status="CANONICAL_WITH_LEGACY_WARNING", reason=fill["direction_warning"]
                    )
            except Exception as exc:
                item.update(
                    status="QUARANTINED",
                    reason=str(exc)
                    if isinstance(exc, AccountReadError)
                    else "Unsupported fill schema: " + type(exc).__name__,
                )
            entries.append(item)
        # Contradictory copies of the same immutable ID cannot be resolved by ordering.
        by_id = {}
        for item in entries:
            f = item["normalized"]
            if f is not None:
                sign = (1 if f["side"] == "yes" else -1) * (1 if f["action"] == "buy" else -1)
                economic = (
                    f["ticker"],
                    sign,
                    f["quantity"],
                    round(f["price"] if f["action"] == "buy" else 1 - f["price"], 12),
                    f["fees"],
                    f["at"],
                    f["order_id"],
                )
                by_id.setdefault(item["fill_id"], set()).add(economic)
        counts_by_id = {}
        invalid_ids = set()
        for item in entries:
            identifier = item["fill_id"]
            counts_by_id[identifier] = counts_by_id.get(identifier, 0) + 1
            if item["status"] == "QUARANTINED":
                invalid_ids.add(identifier)
        for item in entries:
            identifier = item["fill_id"]
            if counts_by_id[identifier] > 1 and (
                identifier in invalid_ids or len(by_id.get(identifier, set())) > 1
            ):
                item.update(status="QUARANTINED", reason="CONTRADICTORY_DUPLICATE_FILL_ID")
        positions = []
        for raw in snapshot["positions"]:
            ticker = raw.get("ticker") or raw.get("market_ticker")
            if not isinstance(ticker, str):
                raise AccountReadError("Position ticker missing; reconciliation withheld")
            qty = float(decimal(raw.get("position_fp", raw.get("position"))))
            positions.append((ticker, qty))
        if len({t for t, _ in positions}) != len(positions):
            raise AccountReadError("Duplicate position tickers; reconciliation withheld")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in entries:
                db.execute(
                    "INSERT OR IGNORE INTO monitor_account_fill_audit VALUES (?,?,?,?,?,?,?,?)",
                    (
                        item["fill_id"],
                        item["digest"],
                        "broker_fill",
                        item["ticker"],
                        item["status"],
                        item["reason"],
                        item["payload"],
                        observed,
                    ),
                )
                db.execute(
                    "INSERT OR REPLACE INTO monitor_account_fill_resolution VALUES (?,?,?,?,?,?)",
                    (
                        item["fill_id"],
                        item["ticker"],
                        item["order_id"],
                        item["status"],
                        item["reason"],
                        observed,
                    ),
                )
                if item["status"] == "QUARANTINED":
                    continue
                fill = item["normalized"]
                existing = db.execute(
                    "SELECT payload FROM monitor_account_fills WHERE fill_id=?", (fill["id"],)
                ).fetchone()
                updated = json.dumps(fill)
                if existing and existing[0] != updated:
                    old_hash = hashlib.sha256(existing[0].encode()).hexdigest()
                    db.execute(
                        "INSERT OR IGNORE INTO monitor_account_fill_audit VALUES (?,?,?,?,?,?,?,?)",
                        (
                            fill["id"],
                            old_hash,
                            "previous_normalized",
                            fill["ticker"],
                            "SUPERSEDED_NORMALIZATION",
                            "Previous cached interpretation retained",
                            existing[0],
                            observed,
                        ),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO monitor_account_fills VALUES (?,?)",
                    (fill["id"], updated),
                )
            fills = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT f.payload FROM monitor_account_fills f LEFT JOIN monitor_account_fill_resolution r ON r.fill_id=f.fill_id WHERE COALESCE(r.status,'READY')!='QUARANTINED'"
                )
            ]
            quarantined = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT ticker FROM monitor_account_fill_resolution WHERE status='QUARANTINED'"
                )
            }
            legacy_warnings = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT ticker FROM monitor_account_fill_resolution WHERE status='CANONICAL_WITH_LEGACY_WARNING'"
                )
            }
            prior = {
                r["ticker"]: r
                for r in db.execute("SELECT * FROM monitor_account_holdings WHERE status='OPEN'")
            }
            active = {}
            for ticker, signed in positions:
                if abs(signed) < 1e-9:
                    continue
                side = "up" if signed > 0 else "down"
                q = abs(signed)
                basis = fifo_basis(fills, ticker, signed)
                if ticker in quarantined or "" in quarantined:
                    basis = {
                        "verified": False,
                        "reason": "Fill records quarantined; entry costs, fees and P&L unavailable until direction/history is resolved",
                    }
                elif ticker in legacy_warnings:
                    basis["reason"] = (
                        (basis.get("reason") or basis.get("method") or "")
                        + "; valid canonical direction used; deprecated discrepancies preserved in fill audit"
                    )
                old = prior.get(ticker)
                if old and old["side"] != side:
                    db.execute(
                        "UPDATE monitor_account_holdings SET status='CLOSED',quantity=0,updated_at=? WHERE id=?",
                        (observed, old["id"]),
                    )
                    db.execute(
                        "INSERT INTO monitor_account_events (holding_id,kind,observed_at,quantity) VALUES (?,?,?,?)",
                        (old["id"], "CONFIRMED_SIDE_CHANGE", observed, 0),
                    )
                    old = None
                values = (
                    q,
                    basis.get("entry_price"),
                    basis.get("entry_fees"),
                    basis.get("entered_at"),
                    int(basis["verified"]),
                    basis.get("reason", basis.get("method")),
                    observed,
                )
                if old:
                    db.execute(
                        """UPDATE monitor_account_holdings SET quantity=?,entry_price=?,entry_fees=?,entered_at=?,
                        basis_verified=?,basis_reason=?,updated_at=? WHERE id=?""",
                        (*values, old["id"]),
                    )
                    identifier = old["id"]
                    if abs(old["quantity"] - q) > 1e-9:
                        kind = (
                            "CONFIRMED_REDUCTION" if q < old["quantity"] else "CONFIRMED_INCREASE"
                        )
                        db.execute(
                            "INSERT INTO monitor_account_events (holding_id,kind,observed_at,quantity) VALUES (?,?,?,?)",
                            (identifier, kind, observed, q),
                        )
                else:
                    cur = db.execute(
                        """INSERT INTO monitor_account_holdings
                        (ticker,side,quantity,entry_price,entry_fees,entered_at,basis_verified,basis_reason,updated_at,first_seen,status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,'OPEN')""",
                        (ticker, side, *values, observed),
                    )
                    identifier = cur.lastrowid
                active[ticker] = identifier
            for ticker, old in prior.items():
                if ticker not in active:
                    db.execute(
                        "UPDATE monitor_account_holdings SET status='CLOSED',quantity=0,updated_at=? WHERE id=?",
                        (observed, old["id"]),
                    )
                    db.execute(
                        "INSERT INTO monitor_account_events (holding_id,kind,observed_at,quantity) VALUES (?,?,?,?)",
                        (old["id"], "CONFIRMED_EXIT_OR_ZERO_POSITION", observed, 0),
                    )
            # Store only account data, never the authenticated client's headers/key metadata identifiers.
            public = {
                k: snapshot[k]
                for k in ("observed_at", "balance", "orders", "permissions", "complete")
            }
            public["fill_count"] = len(fills)
            counts = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT status,COUNT(*) FROM monitor_account_fill_resolution GROUP BY status"
                )
            }
            public["fill_reconciliation"] = {
                "broker_records_this_sync": len(entries),
                "eligible_fills": len(fills),
                "quarantined_fills": counts.get("QUARANTINED", 0),
                "canonical_legacy_discrepancies": counts.get("CANONICAL_WITH_LEGACY_WARNING", 0),
                "audit_records": db.execute(
                    "SELECT COUNT(*) FROM monitor_account_fill_audit"
                ).fetchone()[0],
                "notice": "Canonical fields are authoritative. Deprecated conflicts are audited. Quarantined records are preserved and excluded from cost basis; no direction is guessed.",
            }
            public["confirmed_fill_source"] = "portfolio/fills, not order status"
            db.execute(
                "INSERT OR REPLACE INTO monitor_account_state VALUES (1,?)", (json.dumps(public),)
            )
        return fills

    def alert(self, guidance, now):
        if guidance["guidance"] == "WAIT":
            return "account-wait-" + str(guidance["holding_id"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM monitor_account_guidance_events WHERE holding_id=? ORDER BY id DESC LIMIT 1",
                (guidance["holding_id"],),
            ).fetchone()
            if (
                row
                and row["guidance"] == guidance["guidance"]
                and row["quantity"] == guidance["quantity"]
            ):
                return row["id"]
            cur = db.execute(
                "INSERT INTO monitor_account_guidance_events (holding_id,guidance,quantity,observed_at) VALUES (?,?,?,?)",
                (
                    guidance["holding_id"],
                    guidance["guidance"],
                    guidance["quantity"],
                    now.isoformat(),
                ),
            )
            return cur.lastrowid

    def snapshot(self):
        cached = self._snapshot_cache
        if cached is None:
            with self._view_lock:
                if self._snapshot_cache is None:
                    self._refresh_snapshot()
                cached = self._snapshot_cache
        # Callers add transient quotes/guidance; never mutate the cached account.
        return deepcopy(cached)

    def completed_history(self, before=None, limit=20):
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("History cursor must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History page size must be 1..100")
        self.snapshot()  # Rehydrate once after restart, never on every request.
        rows = self._completed_cache
        eligible = [r for r in rows if before is None or r["history_id"] < before]
        page = eligible[:limit]
        return deepcopy(
            {
                "trades": page,
                "next_before_id": page[-1]["history_id"] if len(eligible) > limit else None,
                "has_more": len(eligible) > limit,
                "total": len(rows),
                "notice": "Actual confirmed-fill FIFO history, separate from simulations. Unknown fees remain unavailable.",
            }
        )

    def _refresh_snapshot(self, all_fills=None):
        with storage_connect(self.path, readonly=True) as db:
            row = db.execute("SELECT payload FROM monitor_account_state WHERE id=1").fetchone()
            data = json.loads(row[0]) if row else {}
            # The main view needs every open holding, plus only recent closed
            # cards. Full economic history remains in the database.
            data["holdings"] = [
                dict(r)
                for r in db.execute(
                    """SELECT h.*,COALESCE(c.enabled,0) selected FROM monitor_account_holdings h
                    LEFT JOIN monitor_account_choices c ON c.holding_id=h.id
                    WHERE h.status='OPEN' OR h.id IN
                        (SELECT id FROM monitor_account_holdings WHERE status='CLOSED' ORDER BY id DESC LIMIT 50)
                    ORDER BY h.id DESC"""
                )
            ]
            data["recent_fills"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT f.payload FROM monitor_account_fills f LEFT JOIN monitor_account_fill_resolution r ON r.fill_id=f.fill_id WHERE COALESCE(r.status,'READY')!='QUARANTINED' ORDER BY f.rowid DESC LIMIT 50"
                )
            ]
            data["recent_events"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM monitor_account_events ORDER BY id DESC LIMIT 30"
                )
            ]
            if all_fills is None:
                all_fills = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT f.payload FROM monitor_account_fills f LEFT JOIN monitor_account_fill_resolution r ON r.fill_id=f.fill_id WHERE COALESCE(r.status,'READY')!='QUARANTINED'"
                    )
                ]
            bad_tickers = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT ticker FROM monitor_account_fill_resolution WHERE status='QUARANTINED'"
                )
            }
            bad_orders = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT order_id FROM monitor_account_fill_resolution WHERE status='QUARANTINED'"
                )
            }
            limits = db.execute(
                "SELECT payload FROM monitor_account_settings WHERE name='limits'"
            ).fetchone()
            data["limits"] = json.loads(limits[0]) if limits else None
        unreconciled = {
            h["ticker"]
            for h in data["holdings"]
            if h["status"] == "OPEN" and not h["basis_verified"]
        }
        economic_fields = (
            "id",
            "ticker",
            "side",
            "action",
            "quantity",
            "price",
            "fees",
            "at",
            "order_id",
        )
        signature = frozenset(tuple(fill.get(key) for key in economic_fields) for fill in all_fills)
        if signature != self._cycles_signature:
            self._cycles_cache = confirmed_cycles(all_fills)
            self._cycles_signature = signature
        completed = [
            c
            for c in self._cycles_cache
            if c["status"] == "CLOSED"
            and c["ticker"] not in unreconciled
            and c["ticker"] not in bad_tickers
            and "" not in bad_tickers
        ]
        # Stable journal-local IDs make history pagination independent of
        # newly arriving fills. This is an additive derived index, never a fill.
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO monitor_account_completed_index(cycle_id) VALUES (?)",
                [
                    (c["cycle_id"],)
                    for c in sorted(completed, key=lambda c: (c["closed_at"], c["cycle_id"]))
                ],
            )
            identities = {
                r["cycle_id"]: r["id"]
                for r in db.execute("SELECT * FROM monitor_account_completed_index")
            }
        for cycle in completed:
            cycle["history_id"] = identities[cycle["cycle_id"]]
        completed.sort(key=lambda c: c["history_id"], reverse=True)
        data["completed_trades"] = completed[:50]
        data["completed_trades_total"] = len(completed)
        data["completed_history_has_more"] = len(completed) > 50
        data["completed_history_next_before_id"] = (
            completed[49]["history_id"] if len(completed) > 50 else None
        )
        by_order = {}
        for fill in all_fills:
            by_order.setdefault(fill["order_id"], []).append(fill)
        data["pending_orders"] = []
        for order in data.get("orders", []):
            if order.get("status") not in ("resting", "pending"):
                continue
            order_id = order.get("order_id")
            actual = by_order.get(order_id, [])
            data["pending_orders"].append(
                {
                    "order_id": order_id,
                    "ticker": order.get("ticker"),
                    "status": "FILL DETAILS UNVERIFIED"
                    if order_id in bad_orders or "" in bad_orders
                    else "PARTIALLY FILLED"
                    if actual
                    else "PENDING / UNFILLED",
                    "confirmed_filled_quantity": None
                    if order_id in bad_orders or "" in bad_orders
                    else sum(f["quantity"] for f in actual),
                    "remaining_quantity": order.get(
                        "remaining_count_fp", order.get("remaining_count")
                    ),
                    "invested_from_unfilled_quantity": 0,
                    "note": "Only confirmed fills contribute to holding cost; remaining quantity is not invested.",
                }
            )
        self._completed_cache = completed
        self._snapshot_cache = data


def account_guidance(holding, account, quote, state, now, defaults, fresh_reconciliation):
    result = {
        "holding_id": holding["id"],
        "ticker": holding["ticker"],
        "side": holding["side"],
        "quantity": holding["quantity"],
        "guidance": "WAIT",
        "reason": "Fresh startup/reconnect account reconciliation required.",
        "timestamp": now.isoformat(),
        "executable_bid": None,
        "estimated_impact_after_costs": None,
        "exit_trigger": None,
        "forecast_assessment": "UNAVAILABLE",
        "forecast_probability": None,
        "forecast_confirmation": None,
        "add_suggestion": None,
    }
    if not fresh_reconciliation:
        return result
    try:
        if not 0 <= (now - timestamp(account["observed_at"])).total_seconds() <= 20:
            raise ValueError("Account data is stale; no position guidance")
        if not holding["basis_verified"]:
            raise ValueError(holding["basis_reason"])
        if not quote or not 0 <= (now - timestamp(quote["observed_at"])).total_seconds() <= 5:
            raise ValueError("Recorded contract quotes are stale or unavailable")
        if quote.get("book_diagnostic"):
            raise ValueError(quote["book_diagnostic"])
        q = quote["quotes"][holding["side"]]
        qty = holding["quantity"]
        if q["bid_size"] < qty:
            raise ValueError("Insufficient executable bid depth for the held quantity")
        if quote["fee_type"] not in ("quadratic", "quadratic_with_maker_fees"):
            raise ValueError("Exit fees unverified")
        bid = number(q["bid"], 0, 1)
        sell = max(0, bid - defaults["slippage"])
        mult = number(quote["fee_multiplier"], 0, 100)
        exit_fee = math.ceil(0.07 * mult * qty * sell * (1 - sell) * 100 - 1e-12) / 100
        impact = qty * (sell - holding["entry_price"]) - holding["entry_fees"] - exit_fee
        result.update(
            guidance="HOLD",
            reason="No cost-adjusted exit threshold reached; add suggestions require configured limits and validated incremental evidence.",
            executable_bid=bid,
            estimated_impact_after_costs=impact,
            estimated_exit_fee=exit_fee,
            quote_timestamp=quote["observed_at"],
        )
        forecast = position_forecast_assessment(holding, state, now)
        result.update(
            forecast_assessment=forecast["assessment"],
            forecast_probability=forecast["probability"],
            forecast_confirmation=state.get("account_forecast_confirmation", {}).get(
                str(holding["id"])
            ),
        )
        # A confirmed account holding is not automatically a dashboard scalp.
        # Its broker fill time must never inherit the global 60-second scalp
        # horizon. Only a user-recorded manual scalp has an explicit max_hold.
        # Account guidance therefore reacts to cost-adjusted price thresholds;
        # it does not manufacture a time limit the trader never selected.
        unit_impact = impact / qty
        confirmed_adverse = (
            forecast["assessment"] == "ROBUST_ADVERSE"
            and bool(result["forecast_confirmation"])
        )
        if unit_impact <= -defaults["stop"]:
            result.update(
                guidance="CONSIDER EXIT",
                exit_trigger="STOP",
                reason="The executable bid after estimated slippage and fees reached the configured loss threshold. This hard risk warning does not wait for an experimental forecast.",
            )
            return result
        if confirmed_adverse and unit_impact >= 0:
            result.update(
                guidance="CONSIDER EXIT",
                exit_trigger="FORECAST_DETERIORATION",
                reason="Fresh same-contract estimates repeatedly turned against this side while an executable cost-adjusted gain remains. The estimate is experimental; guidance is not an order or guaranteed fill.",
            )
            return result
        if unit_impact >= defaults["target"]:
            if forecast["assessment"] == "ROBUST_SUPPORT":
                result.update(
                    guidance="HOLD",
                    reason="The configured profit target is reached, but fresh conservative same-contract estimates still support this side. HOLD is experimental and will be reassessed on each new model update.",
                )
                return result
            if forecast["assessment"] == "ROBUST_ADVERSE" and not confirmed_adverse:
                result.update(
                    guidance="HOLD",
                    reason="The profit target is reached and the forecast has started to weaken, but persistence confirmation is still pending; one noisy update cannot trigger an exit alert.",
                )
                return result
            result.update(
                guidance="CONSIDER EXIT",
                exit_trigger="TARGET_WITHOUT_FORECAST_SUPPORT",
                reason="The executable cost-adjusted profit target is reached and no robust fresh forecast supports extending the hold. Guidance is not an order or guaranteed fill.",
            )
            return result
        result["add_suggestion"] = incremental_add(holding, account, quote, state, defaults, now)
        if result["add_suggestion"].get("eligible"):
            result.update(
                guidance="CONSIDER ADDING",
                reason="Positive incremental expected value under explicit limits and independent held-out evidence; never based on a loss.",
            )
    except (KeyError, ValueError, TypeError) as exc:
        result["reason"] = str(exc)
    return result


def position_forecast_assessment(holding, state, now):
    """Conservative held-side interpretation; never turns stale estimates into advice."""
    unavailable = {"assessment": "UNAVAILABLE", "probability": None, "reason": None}
    try:
        market = state.get("market") or {}
        forecast = state.get("prediction_engine") or {}
        if (
            state.get("stale", True)
            or market.get("ticker") != holding["ticker"]
            or forecast.get("ticker") != holding["ticker"]
            or forecast.get("status") != "EXPERIMENTAL"
            or not 0 <= (now - timestamp(forecast["updated_at"])).total_seconds() <= 5
        ):
            return unavailable
        up = number(forecast["up"], 0, 1)
        bounds = state.get("model", {}).get("settlement_probability_sensitivity")
        if not bounds or len(bounds) != 2:
            return unavailable
        low, high = number(bounds[0], 0, 1), number(bounds[1], 0, 1)
        if low > high:
            return unavailable
        if holding["side"] == "up":
            probability, conservative, adverse_upper = up, low, high
        elif holding["side"] == "down":
            probability, conservative, adverse_upper = 1 - up, 1 - high, 1 - low
        else:
            return unavailable
        assessment = (
            "ROBUST_SUPPORT" if conservative >= 0.55
            else "ROBUST_ADVERSE" if adverse_upper <= 0.45
            else "UNCERTAIN"
        )
        return {
            "assessment": assessment,
            "probability": probability,
            "conservative_probability": conservative,
            "adverse_upper_probability": adverse_upper,
            "updated_at": forecast["updated_at"],
            "reason": "Experimental same-contract estimate with sensitivity bounds",
        }
    except (KeyError, ValueError, TypeError, AttributeError):
        return unavailable


def incremental_add(holding, account, quote, state, defaults, now):
    rejected = lambda reason: {"eligible": False, "reason": reason}
    limits = account.get("limits")
    if not limits:
        return rejected(
            "Adding withheld: explicit position/exposure/cash limits are not configured"
        )
    if state.get("stale") or (state.get("market") or {}).get("ticker") != holding["ticker"]:
        return rejected("Adding withheld: no fresh independent forecast for this held contract")
    info = state.get("model", {})
    learning = info.get("learning_status", {})
    report = learning.get("heldout_report", {})
    # High model probability, a losing holding or a Monte Carlo interval are not empirical edge evidence.
    test = report.get("test", {})
    selected = test.get("selected_candidate", {})
    base = test.get("zero_drift", {})
    if (
        not learning.get("trained_candidate_loaded")
        or selected.get("samples", 0) < 100
        or selected.get("brier", 1) >= base.get("brier", 0)
        or selected.get("log_loss", 99) >= base.get("log_loss", 0)
    ):
        return rejected(
            "Adding withheld: adequate independent chronological evidence is unavailable; edge unverified"
        )
    bounds = info.get("settlement_probability_sensitivity")
    if not bounds:
        return rejected("Adding withheld: outcome uncertainty unavailable")
    p = bounds[0] if holding["side"] == "up" else 1 - bounds[1]
    q = limits["max_add_quantity"]
    offer = quote["quotes"][holding["side"]]
    ask = offer["ask"] + defaults["slippage"]
    if ask >= 1 or offer["ask_size"] < q:
        return rejected("Adding withheld: executable depth/price cap inadequate")
    fee_total = math.ceil(0.07 * quote["fee_multiplier"] * q * ask * (1 - ask) * 100 - 1e-12) / 100
    ev = q * (p - ask) - fee_total
    cost = q * ask + fee_total
    if ev <= 0:
        return rejected("Adding withheld: conservative incremental EV after costs is non-positive")
    reserved = 0
    contract_reserved = 0
    try:
        for order in account.get("orders", []):
            if order.get("status") in ("resting", "pending"):
                remaining = float(
                    decimal(order.get("remaining_count_fp", order.get("remaining_count")))
                )
                reserved += remaining
                if order.get("ticker") == holding["ticker"]:
                    contract_reserved += remaining
    except (AccountReadError, TypeError):
        return rejected("Adding withheld: outstanding-order exposure is unverified")
    total = sum(h["quantity"] for h in account["holdings"] if h["status"] == "OPEN")
    balance = account["balance"]
    cash = (
        float(decimal(balance["balance_dollars"]))
        if balance.get("balance_dollars") is not None
        else float(decimal(balance["balance"])) / 100
    )
    if (
        holding["quantity"] + contract_reserved + q > limits["max_per_contract_quantity"]
        or total + reserved + q > limits["max_account_quantity"]
        or cost > limits["max_add_cost_dollars"]
        or cash - cost < limits["minimum_cash_reserve"]
    ):
        return rejected(
            "Adding withheld: proposed quantity would violate configured exposure/cost/cash limits"
        )
    return {
        "eligible": True,
        "quantity": q,
        "entry_cap": ask,
        "incremental_expected_net_pnl": ev,
        "estimated_cost": cost,
        "reason": "Conditional estimate only; no execution. Loss/P&L is not an eligibility input.",
    }


def live_trade_cards(account, quotes, state, now, defaults, fresh):
    cards = []
    guidance = {g["holding_id"]: g for g in account.get("guidance", [])}
    for h in account.get("holdings", []):
        if h["status"] != "OPEN" or not h["ticker"].startswith("KXBTC15M-"):
            continue
        q = h["quantity"]
        entry = h["entry_price"]
        fees = h["entry_fees"]
        quote = quotes.get(h["ticker"])
        cost = q * entry + fees if entry is not None and fees is not None else None
        card = {
            "holding_id": h["id"],
            "contract": h["ticker"],
            "side": h["side"],
            "filled_quantity": q,
            "conditional_winning_settlement_payout": q,
            "average_entry_price": entry,
            "entry_fees": fees,
            "total_entry_cost": cost,
            "current_executable_exit_price": None,
            "estimated_exit_fee": None,
            "estimated_exit_net_pnl": None,
            "potential_target_net_profit": None,
            "target_bid": None,
            "conditional_winning_settlement_net_profit": q - cost if cost is not None else None,
            "conditional_settlement_label": "Only if these remaining contracts settle as winners; not expected or guaranteed profit; assumes no redemption fee",
            "remaining_cost_at_risk": cost,
            "risk_note": "Remaining cost basis can be lost; the stop is not guaranteed protection",
            "contract_seconds_remaining": max(
                0, (timestamp(quote["closes_at"]) - now).total_seconds()
            )
            if quote and quote.get("closes_at")
            else None,
            "guidance": "WAIT",
            "reason": "Choose this holding for guidance; no signal is assumed to be a purchase.",
            "timestamp": now.isoformat(),
            "account_last_sync": account.get("observed_at"),
            "values_stale": not fresh,
            "source": "confirmed account position + confirmed fill history; distinct from dashboard setups/simulations",
            "selected": bool(h["selected"]),
        }
        forecast = state.get("prediction_engine") or {}
        model_ready = (
            fresh
            and not state.get("stale", True)
            and forecast.get("ticker") == h["ticker"]
            and forecast.get("status") == "EXPERIMENTAL"
        )
        probability = forecast.get(h["side"]) if model_ready else None
        card["current_model_probability"] = probability
        card["model_probability_note"] = (
            "Experimental same-contract model estimate, not validated accuracy"
            if probability is not None
            else "No fresh supported forecast for this held contract"
        )
        card["expected_settlement_value_experimental"] = (
            q * probability - cost if probability is not None and cost is not None else None
        )
        if not fresh:
            card[
                "reason"
            ] = "Account disconnected/stale; values are last known and fresh guidance is withheld"
        elif (
            quote
            and 0 <= (now - timestamp(quote["observed_at"])).total_seconds() <= 5
            and not quote.get("book_diagnostic")
        ):
            offer = quote.get("quotes", {}).get(h["side"], {})
            if offer.get("bid_size", 0) >= q:
                bid = offer.get("bid")
                card["current_executable_exit_price"] = bid
                if bid is not None and quote.get("fee_type") in (
                    "quadratic",
                    "quadratic_with_maker_fees",
                ):
                    mult = quote["fee_multiplier"]
                    slip = defaults["slippage"]

                    def net_at(raw):
                        price = max(0, raw - slip)
                        fee_total = (
                            math.ceil(0.07 * mult * q * price * (1 - price) * 100 - 1e-12) / 100
                        )
                        return q * price - cost - fee_total if cost is not None else None, fee_total

                    net, fee_total = net_at(bid)
                    card.update(estimated_exit_fee=fee_total, estimated_exit_net_pnl=net)
                    if cost is not None:
                        for tick in range(1001):
                            price = tick / 1000
                            potential, _ = net_at(price)
                            if potential >= q * defaults["target"] - 1e-9:
                                card.update(target_bid=price, potential_target_net_profit=potential)
                                break
        else:
            card["reason"] = "Current executable quote unavailable or stale"
        if fresh and h["selected"]:
            g = guidance.get(h["id"])
            if g:
                card.update(
                    guidance=g["guidance"] if g["guidance"] != "CONSIDER ADDING" else "HOLD",
                    reason=g["reason"],
                    alert_id=g.get("alert_id"),
                )
        cards.append(card)
    return cards

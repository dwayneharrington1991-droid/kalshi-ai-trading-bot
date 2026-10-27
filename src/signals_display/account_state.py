"""Confirmed account snapshots, explicit guidance opt-in, no execution capability."""
from .storage import connect as storage_connect
from datetime import timedelta
import json
import math
import sqlite3

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
        with self.connect() as db:
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
        if snapshot.get("complete") is not True:
            raise AccountReadError("Incomplete snapshot cannot close/reconcile holdings")
        observed = snapshot["observed_at"]
        timestamp(observed)
        normalized = [normalized_fill(f) for f in snapshot["fills"]]
        if any(timestamp(f["at"]) > timestamp(observed) + timedelta(seconds=5) for f in normalized):
            raise AccountReadError("Future-dated fill rejected; check account clock/schema")
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
            for fill in normalized:
                db.execute(
                    "INSERT OR REPLACE INTO monitor_account_fills VALUES (?,?)",
                    (fill["id"], json.dumps(fill)),
                )
            fills = [
                json.loads(r[0]) for r in db.execute("SELECT payload FROM monitor_account_fills")
            ]
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
            public["confirmed_fill_source"] = "portfolio/fills, not order status"
            db.execute(
                "INSERT OR REPLACE INTO monitor_account_state VALUES (1,?)", (json.dumps(public),)
            )

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
        with self.connect() as db:
            row = db.execute("SELECT payload FROM monitor_account_state WHERE id=1").fetchone()
            data = json.loads(row[0]) if row else {}
            data["holdings"] = [
                dict(r)
                for r in db.execute(
                    """SELECT h.*,COALESCE(c.enabled,0) selected FROM monitor_account_holdings h
                LEFT JOIN monitor_account_choices c ON c.holding_id=h.id ORDER BY h.id DESC"""
                )
            ]
            data["recent_fills"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM monitor_account_fills ORDER BY rowid DESC LIMIT 50"
                )
            ]
            data["recent_fills"] = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM monitor_account_fills ORDER BY rowid DESC LIMIT 50"
                )
            ]
            data["recent_events"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM monitor_account_events ORDER BY id DESC LIMIT 30"
                )
            ]
        all_fills = []
        with self.connect() as db:
            all_fills = [
                json.loads(r[0]) for r in db.execute("SELECT payload FROM monitor_account_fills")
            ]
        unreconciled = {
            h["ticker"]
            for h in data["holdings"]
            if h["status"] == "OPEN" and not h["basis_verified"]
        }
        data["completed_trades"] = [
            c
            for c in confirmed_cycles(all_fills)
            if c["status"] == "CLOSED" and c["ticker"] not in unreconciled
        ]
        data["pending_orders"] = []
        for order in data.get("orders", []):
            if order.get("status") not in ("resting", "pending"):
                continue
            order_id = order.get("order_id")
            actual = [f for f in all_fills if f["order_id"] == order_id]
            data["pending_orders"].append(
                {
                    "order_id": order_id,
                    "ticker": order.get("ticker"),
                    "status": "PARTIALLY FILLED" if actual else "PENDING / UNFILLED",
                    "confirmed_filled_quantity": sum(f["quantity"] for f in actual),
                    "remaining_quantity": order.get(
                        "remaining_count_fp", order.get("remaining_count")
                    ),
                    "invested_from_unfilled_quantity": 0,
                    "note": "Only confirmed fills contribute to holding cost; remaining quantity is not invested.",
                }
            )
        data["limits"] = self.limits()
        return data


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
        timed = holding.get("entered_at") and now >= timestamp(holding["entered_at"]) + timedelta(
            seconds=defaults["horizon"]
        )
        if impact / qty >= defaults["target"] or impact / qty <= -defaults["stop"] or timed:
            result.update(
                guidance="CONSIDER EXIT",
                reason="Confirmed-fill target, stop or maximum holding time reached. Guidance is not an order or guaranteed fill.",
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

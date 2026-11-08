"""Independent SCALP/HOLD evaluation and immutable paper-signal accounting.

This module has no broker dependency and no execution methods.  A displayed
quote is treated as a bounded counterfactual only when its recorded top-level
size supports the proposed integer quantity.  It is never called a real fill.
"""
from __future__ import annotations

from datetime import timedelta
import json
import math

from .edge_scanner import quantity_fee
from .model import number, timestamp
from .scores import score, wilson_interval


SCALP_PROTOCOL = "executable-scalp-edge-v1"
HOLD_PROTOCOL = "executable-settlement-edge-v1"
MAX_TRADE_EXPOSURE_DOLLARS = 1.0
MAX_TOTAL_EXPOSURE_DOLLARS = 5.0
DAILY_LOSS_LIMIT_DOLLARS = 2.0


def _validation(state, strategy):
    learning = (state.get("model") or {}).get("learning_status") or {}
    report = learning.get("scalp_heldout_report" if strategy == "SCALP" else "heldout_report") or {}
    protocol = SCALP_PROTOCOL if strategy == "SCALP" else HOLD_PROTOCOL
    verified = bool(
        report.get("profitable_edge_demonstrated") is True
        and report.get("protocol") == protocol
        and report.get("chronological_out_of_sample") is True
        and report.get("transaction_costs_included") is True
        and report.get("selection_bias_controlled") is True
    )
    return {
        "verified": verified,
        "protocol": protocol,
        "status": "VERIFIED_OUT_OF_SAMPLE" if verified else "SHADOW_UNVALIDATED",
        "reason": (
            "Independent chronological after-cost validation passed"
            if verified
            else f"No reviewed independent {strategy.lower()} validation demonstrates positive edge after costs"
        ),
    }


def _quantity(entry_cost, liquidity, cap):
    try:
        cost = number(entry_cost, 0.000001, 1)
        depth = math.floor(number(liquidity, 1, 1e12))
        limit = number(cap, 0.01, MAX_TRADE_EXPOSURE_DOLLARS)
        return max(0, min(depth, math.floor((limit + 1e-12) / cost)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _blank(strategy, validation, reason):
    return {
        "strategy": strategy,
        "action": "WAIT",
        "side": None,
        "actionable": False,
        "paper_actionable": False,
        "paper_only": True,
        "validation": validation,
        "proposed_entry": None,
        "target_exit": None,
        "stop_exit": None,
        "quantity": None,
        "hypothetical_exposure": None,
        "maximum_potential_loss": None,
        "estimated_net_profit": None,
        "expected_value": None,
        "confidence": None,
        "reason": reason,
    }


def evaluate(
    state,
    *,
    max_exposure=MAX_TRADE_EXPOSURE_DOLLARS,
    max_total_exposure=MAX_TOTAL_EXPOSURE_DOLLARS,
    daily_loss_limit=DAILY_LOSS_LIMIT_DOLLARS,
    paper_performance=None,
):
    """Compare independently gated SCALP and HOLD counterfactuals."""
    max_exposure = number(max_exposure, 0.01, MAX_TRADE_EXPOSURE_DOLLARS)
    max_total_exposure = number(
        max_total_exposure, max_exposure, MAX_TOTAL_EXPOSURE_DOLLARS
    )
    daily_loss_limit = number(daily_loss_limit, 0.01, DAILY_LOSS_LIMIT_DOLLARS)
    market = state.get("market") or {}
    guardian = (((state.get("decision_intelligence") or {}).get("components") or {}).get("risk_guardian") or {})
    global_blockers = []
    if state.get("stale", True):
        global_blockers.append("STALE_DATA")
    if guardian.get("blockers"):
        global_blockers.append("RISK_GUARDIAN_BLOCKED")
    if (guardian.get("signal_kill_switch") or {}).get("active"):
        global_blockers.append("SIGNAL_KILL_SWITCH")
    paper_performance = paper_performance or {}
    daily_pnl = sum(
        (paper_performance.get(strategy) or {}).get("today_net_profit") or 0
        for strategy in ("SCALP", "HOLD")
    )
    portfolio = paper_performance.get("portfolio") or {}
    open_exposure = portfolio.get("open_exposure") or 0
    open_sides = set(portfolio.get("open_economic_sides") or ())
    if daily_pnl <= -daily_loss_limit:
        global_blockers.append("PAPER_DAILY_LOSS_LIMIT")
    if open_exposure >= max_total_exposure:
        global_blockers.append("PAPER_MAXIMUM_TOTAL_EXPOSURE")

    hold_validation = _validation(state, "HOLD")
    scalp_validation = _validation(state, "SCALP")
    hold = _blank("HOLD", hold_validation, hold_validation["reason"])
    scalp = _blank("SCALP", scalp_validation, scalp_validation["reason"])
    edge = state.get("edge_scanner") or {}
    monitor = state.get("scalping_monitor") or {}
    multiplier = market.get("fee_multiplier")
    opening = (state.get("prediction_engine") or {}).get("initial") or {}
    opening_direction = opening.get("direction")
    opening_probability = opening.get("up")
    opening_candidate = bool(
        str(opening.get("model", "")).startswith("opening-candidate/")
        and opening_direction in {"UP", "DOWN"}
        and type(opening_probability) in (int, float)
        and math.isfinite(opening_probability)
        and 0 <= opening_probability <= 1
        and (not opening.get("ticker") or opening.get("ticker") == market.get("ticker"))
    )

    # HOLD: payout-at-settlement economics from an independently calibrated
    # probability and the executable ask, including fee and slippage.
    hold_options = []
    hold_rejections = {
        "missing_executable_quote_or_probability": 0,
        "missing_fee_or_liquidity": 0,
        "edge_below_minimum": 0,
        "spread_too_wide": 0,
        "exposure_below_one_contract": 0,
    }
    for side, item in (edge.get("sides") or {}).items():
        item = dict(item)
        if not item.get("executable"):
            hold_rejections["missing_executable_quote_or_probability"] += 1
            continue
        spread = item.get("spread")
        entry_cost = (item.get("entry_price_with_slippage") or 0) + (item.get("estimated_fee_per_contract") or 0)
        if opening_candidate:
            expected_side = "up" if opening_direction == "UP" else "down"
            if side != expected_side:
                continue
            probability = opening_probability if side == "up" else 1 - opening_probability
            item["model_probability"] = probability
            item["net_expected_profit_per_contract"] = probability - entry_cost
            item["paper_signal_source"] = "immutable_candlestick_opening_candidate"
        else:
            item["paper_signal_source"] = "settlement_shadow_candidate"
        ev = item.get("net_expected_profit_per_contract")
        liquidity = item.get("available_liquidity")
        if liquidity is None or market.get("fee_multiplier") is None:
            hold_rejections["missing_fee_or_liquidity"] += 1
        if f"{market.get('ticker')}:{side}" in open_sides:
            hold_rejections.setdefault("no_averaging_down", 0)
            hold_rejections["no_averaging_down"] += 1
            continue
        available_exposure = min(max_exposure, max_total_exposure - open_exposure)
        qty = _quantity(entry_cost, liquidity, available_exposure)
        if not qty:
            hold_rejections["exposure_below_one_contract"] += 1
        if type(ev) not in (int, float) or ev < 0.02:
            hold_rejections["edge_below_minimum"] += 1
        if type(spread) not in (int, float) or spread > 0.08:
            hold_rejections["spread_too_wide"] += 1
        if qty and type(ev) in (int, float) and ev >= 0.02 and type(spread) in (int, float) and spread <= 0.08:
            hold_options.append((ev, side, item, entry_cost, qty))
    if hold_options:
        ev, side, item, entry_cost, qty = max(hold_options)
        if not global_blockers:
            hold.update(
                action="PAPER BUY UP" if side == "up" else "PAPER BUY DOWN", side=side,
                actionable=False, paper_actionable=True,
                proposed_entry=item["market_purchase_price"], target_exit=1.0,
                quantity=qty, hypothetical_exposure=qty * entry_cost,
                maximum_potential_loss=qty * entry_cost,
                estimated_net_profit=qty * ev, expected_value=qty * ev,
                confidence=item.get("model_probability"),
                signal_source=item["paper_signal_source"],
                reason=(
                    "Automated paper-only settlement candidate after ask, slippage, fee and displayed-depth checks; "
                    + hold_validation["status"]
                ),
            )
        else:
            hold["reason"] = "; ".join(global_blockers or [hold_validation["reason"]])

    # SCALP: target-before-stop/time estimate is a distinct prediction problem.
    scalp_options = []
    scalp_rejections = {
        "incomplete_target_stop_quote": 0,
        "unknown_fees": 0,
        "exposure_below_one_contract": 0,
        "edge_below_minimum": 0,
        "spread_too_wide": 0,
        "probability_below_minimum": 0,
    }
    for side, item in (monitor.get("sides") or {}).items():
        ask = item.get("ask")
        target = item.get("target_bid")
        stop = item.get("stop_bid")
        spread = item.get("spread")
        probability = item.get("target_probability")
        ev = item.get("net_expected_profit")
        liquidity = item.get("ask_size")
        if not all(type(x) in (int, float) for x in (ask, target, stop, spread, probability, ev)):
            scalp_rejections["incomplete_target_stop_quote"] += 1
            continue
        entry_fee = quantity_fee(ask, 1, multiplier) if type(multiplier) in (int, float) else None
        if entry_fee is None:
            scalp_rejections["unknown_fees"] += 1
            continue
        entry_cost = ask + entry_fee + (item.get("slippage") or 0)
        if f"{market.get('ticker')}:{side}" in open_sides:
            scalp_rejections.setdefault("no_averaging_down", 0)
            scalp_rejections["no_averaging_down"] += 1
            continue
        available_exposure = min(max_exposure, max_total_exposure - open_exposure)
        qty = _quantity(entry_cost, liquidity, available_exposure)
        if not qty:
            scalp_rejections["exposure_below_one_contract"] += 1
        if ev < 0.01:
            scalp_rejections["edge_below_minimum"] += 1
        if spread > 0.03:
            scalp_rejections["spread_too_wide"] += 1
        if probability < 0.60:
            scalp_rejections["probability_below_minimum"] += 1
        if qty and ev >= 0.01 and spread <= 0.03 and probability >= 0.60:
            scalp_options.append((ev, side, item, entry_cost, qty))
    if scalp_options:
        ev, side, item, entry_cost, qty = max(scalp_options)
        if not global_blockers:
            scalp.update(
                action="PAPER BUY UP" if side == "up" else "PAPER BUY DOWN", side=side,
                actionable=False, paper_actionable=True,
                proposed_entry=item["ask"], target_exit=item["target_bid"],
                stop_exit=item["stop_bid"], quantity=qty,
                hypothetical_exposure=qty * entry_cost,
                maximum_potential_loss=qty * max(0, entry_cost - item["stop_bid"]),
                estimated_net_profit=qty * ev, expected_value=qty * ev,
                confidence=item["target_probability"],
                holding_seconds=item.get("holding_seconds"),
                exit_slippage=item.get("slippage"),
                signal_source="scalp_shadow_probability",
                reason=(
                    "Automated paper-only target-before-stop candidate after spread, fees, slippage and depth; "
                    + scalp_validation["status"]
                ),
            )
        else:
            scalp["reason"] = "; ".join(global_blockers or [scalp_validation["reason"]])

    if not hold_options:
        hold["reason"] = "WAIT: " + ", ".join(
            f"{name}={count}" for name, count in hold_rejections.items() if count
        ) if any(hold_rejections.values()) else "WAIT: no compatible settlement-side market inputs"
    if not scalp_options:
        scalp["reason"] = "WAIT: " + ", ".join(
            f"{name}={count}" for name, count in scalp_rejections.items() if count
        ) if any(scalp_rejections.values()) else "WAIT: no compatible target-before-stop market inputs"

    actionable = [value for value in (scalp, hold) if value["paper_actionable"]]
    selected = max(actionable, key=lambda value: value["expected_value"]) if actionable else None
    return {
        "timestamp": state.get("now"),
        "ticker": market.get("ticker"),
        "recommended_strategy": selected["strategy"] if selected else "WAIT",
        "action": selected["action"] if selected else "WAIT",
        "reason": selected["reason"] if selected else (global_blockers[0] if global_blockers else "Neither independently validated strategy has verified positive after-cost edge"),
        "strategies": {"SCALP": scalp, "HOLD": hold},
        "input_diagnostics": {
            "global_blockers": global_blockers,
            "HOLD": hold_rejections,
            "SCALP": scalp_rejections,
        },
        "paper_limits": {
            "maximum_exposure_per_signal": max_exposure,
            "maximum_total_exposure": max_total_exposure,
            "daily_loss_limit": daily_loss_limit,
            "no_averaging_down": True,
        },
        "paper_performance": paper_performance,
        "execution": "AUTOMATED_PAPER_ONLY",
        "execution_capability": False,
        "live_orders_possible": False,
        "fills_are_hypothetical": True,
        "global_blockers": global_blockers,
    }


def create_schema(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS strategy_paper_signals (
        signal_key TEXT PRIMARY KEY,ticker TEXT NOT NULL,strategy TEXT NOT NULL,
        side TEXT NOT NULL,created_at TEXT NOT NULL,deadline TEXT NOT NULL,
        entry REAL NOT NULL,target REAL NOT NULL,stop REAL,quantity INTEGER NOT NULL,
        exposure REAL NOT NULL,max_loss REAL NOT NULL,estimated_profit REAL NOT NULL,
        probability REAL NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS strategy_paper_outcomes (
        signal_key TEXT PRIMARY KEY,evaluated_at TEXT NOT NULL,outcome_type TEXT NOT NULL,
        exit_price REAL NOT NULL,net_pnl REAL NOT NULL,correct INTEGER NOT NULL,
        payload TEXT NOT NULL,FOREIGN KEY(signal_key) REFERENCES strategy_paper_signals(signal_key));
      CREATE TRIGGER IF NOT EXISTS strategy_paper_signals_no_update BEFORE UPDATE ON strategy_paper_signals BEGIN SELECT RAISE(ABORT,'paper signals are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS strategy_paper_signals_no_delete BEFORE DELETE ON strategy_paper_signals BEGIN SELECT RAISE(ABORT,'paper signals are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS strategy_paper_outcomes_no_update BEFORE UPDATE ON strategy_paper_outcomes BEGIN SELECT RAISE(ABORT,'paper outcomes are immutable'); END;
      CREATE TRIGGER IF NOT EXISTS strategy_paper_outcomes_no_delete BEFORE DELETE ON strategy_paper_outcomes BEGIN SELECT RAISE(ABORT,'paper outcomes are immutable'); END;
    """)


def record_and_resolve(db, state, recorded_at):
    evaluation = state.get("dual_strategy") or {}
    market = state.get("market") or {}
    ticker = market.get("ticker")
    now = timestamp(recorded_at) if isinstance(recorded_at, str) else recorded_at
    selected = evaluation.get("strategies", {}).get(evaluation.get("recommended_strategy"))
    if selected and selected.get("paper_actionable") and ticker and not state.get("stale", True):
        created = timestamp(evaluation["timestamp"])
        horizon = selected.get("holding_seconds") or max(1, int(state.get("remaining_seconds") or 1))
        deadline = min(created + timedelta(seconds=horizon), timestamp(market["closes_at"]))
        # One strategy/side entry per contract. Repeated one-second states do
        # not manufacture additional paper positions.
        key = f"{ticker}:{selected['strategy']}:{selected['side']}"
        payload = {**selected, "signal_key": key, "ticker": ticker, "created_at": created.isoformat(), "deadline": deadline.isoformat(), "fill_status": "COUNTERFACTUAL_DISPLAYED_QUOTE"}
        exposure = float(selected["hypothetical_exposure"])
        open_exposure = db.execute(
            """SELECT COALESCE(SUM(s.exposure),0) FROM strategy_paper_signals s
            LEFT JOIN strategy_paper_outcomes o USING(signal_key)
            WHERE o.signal_key IS NULL"""
        ).fetchone()[0]
        same_side_open = db.execute(
            """SELECT 1 FROM strategy_paper_signals s
            LEFT JOIN strategy_paper_outcomes o USING(signal_key)
            WHERE o.signal_key IS NULL AND s.ticker=? AND s.side=? LIMIT 1""",
            (ticker, selected["side"]),
        ).fetchone()
        day = created.date().isoformat()
        today_pnl = db.execute(
            """SELECT COALESCE(SUM(o.net_pnl),0) FROM strategy_paper_outcomes o
            JOIN strategy_paper_signals s USING(signal_key)
            WHERE substr(o.evaluated_at,1,10)=?""",
            (day,),
        ).fetchone()[0]
        risk_ok = (
            exposure <= MAX_TRADE_EXPOSURE_DOLLARS + 1e-12
            and open_exposure + exposure <= MAX_TOTAL_EXPOSURE_DOLLARS + 1e-12
            and today_pnl > -DAILY_LOSS_LIMIT_DOLLARS
            and same_side_open is None
        )
        if risk_ok:
            db.execute("""INSERT OR IGNORE INTO strategy_paper_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                key,ticker,selected["strategy"],selected["side"],created.isoformat(),deadline.isoformat(),
                selected["proposed_entry"],selected["target_exit"],selected.get("stop_exit"),selected["quantity"],
                exposure,selected["maximum_potential_loss"],selected["estimated_net_profit"],
                selected["confidence"],json.dumps(payload,allow_nan=False,separators=(",",":")),
            ))

    open_rows = db.execute("""SELECT s.* FROM strategy_paper_signals s LEFT JOIN strategy_paper_outcomes o USING(signal_key) WHERE o.signal_key IS NULL ORDER BY s.created_at LIMIT 100""").fetchall()
    official = {row[0]: row[1] for row in db.execute("SELECT ticker,outcome FROM forecast_outcomes WHERE origin='official'")}
    for row in open_rows:
        item = dict(row)
        if now <= timestamp(item["created_at"]):
            continue
        quote = ((market.get("quotes") or {}).get(item["side"]) or {}) if ticker == item["ticker"] and not state.get("stale") else {}
        bid, size = quote.get("bid"), quote.get("bid_size")
        exit_price = outcome_type = None
        if item["strategy"] == "HOLD" and item["ticker"] in official:
            won = official[item["ticker"]] == (1 if item["side"] == "up" else 0)
            exit_price, outcome_type = (1.0 if won else 0.0), "OFFICIAL_SETTLEMENT"
        elif item["strategy"] == "SCALP" and type(bid) in (int, float) and type(size) in (int, float) and size >= item["quantity"]:
            if bid >= item["target"]:
                exit_price, outcome_type = bid, "TARGET_OBSERVED"
            elif item["stop"] is not None and bid <= item["stop"]:
                exit_price, outcome_type = bid, "STOP_OBSERVED"
            elif now >= timestamp(item["deadline"]):
                exit_price, outcome_type = bid, "TIME_EXIT_OBSERVED"
        if exit_price is None:
            continue
        multiplier = market.get("fee_multiplier") if ticker == item["ticker"] else 1
        payload = json.loads(item["payload"])
        observed_exit = exit_price
        if outcome_type == "OFFICIAL_SETTLEMENT":
            exit_fee = 0.0
        else:
            exit_price = max(0.0, exit_price - float(payload.get("exit_slippage") or 0))
            exit_fee = quantity_fee(exit_price, item["quantity"], multiplier)
        net = item["quantity"] * exit_price - exit_fee - item["exposure"]
        outcome = {"signal_key": item["signal_key"], "evaluated_at": now.isoformat(), "outcome_type": outcome_type, "observed_exit_bid": observed_exit, "simulated_exit_price": exit_price, "exit_slippage": observed_exit-exit_price, "exit_fee": exit_fee, "net_pnl": net, "actual_fill": False}
        db.execute("INSERT OR IGNORE INTO strategy_paper_outcomes VALUES (?,?,?,?,?,?,?)", (
            item["signal_key"],now.isoformat(),outcome_type,exit_price,net,int(net > 0),json.dumps(outcome,allow_nan=False,separators=(",",":")),
        ))


def performance(db):
    rows = [dict(row) for row in db.execute("""SELECT s.*,o.net_pnl,o.correct,o.outcome_type FROM strategy_paper_signals s LEFT JOIN strategy_paper_outcomes o USING(signal_key) ORDER BY s.created_at""")]
    official_contracts = db.execute(
        "SELECT COUNT(*) FROM forecast_outcomes WHERE origin='official'"
    ).fetchone()[0]
    def summary(strategy):
        signals = [r for r in rows if r["strategy"] == strategy]
        closed = [r for r in signals if r["net_pnl"] is not None]
        pnl = [r["net_pnl"] for r in closed]
        equity = peak = drawdown = 0.0
        for value in pnl:
            equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
        metrics = score([r["probability"] for r in closed], [r["correct"] for r in closed]) if closed else {}
        wins = sum(r["correct"] for r in closed)
        invested = sum(r["exposure"] for r in closed)
        today = timestamp(rows[-1]["created_at"]).date() if rows else None
        today_pnl = sum(
            r["net_pnl"] for r in closed if timestamp(r["created_at"]).date() == today
        ) if today else 0.0
        return {
            "signals": len(signals), "settled_or_observed_outcomes": len(closed), "wins": wins,
            "losses": len(closed)-wins, "win_rate": wins/len(closed) if closed else None,
            "signal_coverage": len({r["ticker"] for r in signals}) / official_contracts if official_contracts else None,
            "win_rate_interval_95": wilson_interval(wins,len(closed)), "net_profit": sum(pnl) if pnl else None,
            "total_return": sum(pnl) / invested if invested else None,
            "maximum_drawdown": drawdown if closed else None, "brier": metrics.get("brier"),
            "today_net_profit": today_pnl,
            "log_loss": metrics.get("log_loss"), "expected_calibration_error": metrics.get("expected_calibration_error"),
            "edge_status": "UNVERIFIED" if len(closed) < 30 else "REQUIRES_INDEPENDENT_REVIEW",
        }
    open_rows = [row for row in rows if row["net_pnl"] is None]
    closed_rows = [row for row in rows if row["net_pnl"] is not None]
    invested = sum(row["exposure"] for row in closed_rows)
    net = sum(row["net_pnl"] for row in closed_rows)
    return {
        "SCALP": summary("SCALP"),
        "HOLD": summary("HOLD"),
        "portfolio": {
            "open_positions": len(open_rows),
            "open_exposure": sum(row["exposure"] for row in open_rows),
            "open_economic_sides": sorted(
                {f"{row['ticker']}:{row['side']}" for row in open_rows}
            ),
            "closed_trades": len(closed_rows),
            "net_profit": net if closed_rows else None,
            "total_return": net / invested if invested else None,
            "maximum_total_exposure": MAX_TOTAL_EXPOSURE_DOLLARS,
            "maximum_trade_exposure": MAX_TRADE_EXPOSURE_DOLLARS,
            "daily_loss_limit": DAILY_LOSS_LIMIT_DOLLARS,
            "no_averaging_down": True,
        },
        "actual_fills": 0,
        "scope": "Immutable displayed-quote counterfactuals; not actual executions",
    }

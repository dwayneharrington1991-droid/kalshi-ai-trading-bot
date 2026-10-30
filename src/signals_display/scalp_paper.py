"""Chronological executable-book paper replay. No broker or execution client.

Quotes are observations, not fills. Results assume displayed liquidity can be
taken after a configured delay; priority, hidden depth and future fills remain
unknown. Missing prices or unobserved barriers never become simulated wins.
"""
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from collections import Counter
from contextlib import closing
import argparse
import json
import math
from pathlib import Path
import sqlite3

from .model import number, timestamp
from .scores import wilson_interval

VERSION = "observed-book-paper-v1"
FEES = {"quadratic", "quadratic_with_maker_fees"}


@dataclass(frozen=True)
class PaperCriteria:
    quantity: float = 1.0
    target: float = 0.05
    stop: float = 0.10
    holding_seconds: float = 60.0
    manual_delay_seconds: float = 3.0
    entry_window_seconds: float = 2.0
    slippage_per_leg: float = 0.01
    maximum_spread: float = 0.03
    maximum_quote_age_seconds: float = 5.0
    maximum_gap_seconds: float = 5.0
    max_position_quantity: float = 5.0
    max_total_cost: float = 10.0
    max_open_positions: int = 1
    opportunity_interval_seconds: float = 60.0
    minimum_target_probability: float = 0.60
    minimum_entered_paths: int = 200

    @classmethod
    def parse(cls, value=None):
        if isinstance(value, cls):
            return value
        if is_dataclass(value):
            value = asdict(value)
        values = {} if value is None else dict(value)
        if set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown paper criteria")
        out = cls(**values)
        for name in ("quantity", "max_position_quantity", "max_total_cost"):
            number(getattr(out, name), 0.01, 1e6)
        for name in ("target", "stop"):
            number(getattr(out, name), 0.000001, 1)
        number(out.slippage_per_leg, 0, 0.1)
        number(out.maximum_spread, 0, 1)
        number(out.holding_seconds, 1, 900)
        number(out.manual_delay_seconds, 0, 30)
        number(out.entry_window_seconds, 0, 30)
        number(out.maximum_quote_age_seconds, 0.001, 5)
        number(out.maximum_gap_seconds, 0.001, 5)
        number(out.opportunity_interval_seconds, 0, 900)
        number(out.minimum_target_probability, 0.5, 1)
        if type(out.max_open_positions) is not int or not 1 <= out.max_open_positions <= 100:
            raise ValueError("Invalid paper position limit")
        if type(out.minimum_entered_paths) is not int or out.minimum_entered_paths < 1:
            raise ValueError("Invalid paper Monte Carlo sample minimum")
        return out


def cost_fee(price, quantity, multiplier):
    """Total estimated taker fee for this observed simulated partial fill."""
    return math.ceil(0.07 * multiplier * quantity * price * (1 - price) * 100 - 1e-12) / 100


def create_schema(db):
    db.executescript(
        """CREATE TABLE IF NOT EXISTS scalp_book_observations (
        id INTEGER PRIMARY KEY,mode TEXT,ticker TEXT,observed_at TEXT,
        generated_at TEXT,recorded_at TEXT,payload TEXT,
        UNIQUE(mode,ticker,generated_at,observed_at));"""
    )
    # Secondary indexes are constructed only by Journal's size-aware offline
    # maintenance path. Never scan the preserved journal during startup.


def _candidate(state):
    candidates = []
    for side, detail in state.get("model", {}).get("scalp_edges", {}).items():
        if side not in ("up", "down"):
            continue
        candidates.append(
            {
                "side": side,
                "model_definition": {
                    "quantity": 1.0,
                    "target": state.get("plans", {}).get(side, {}).get("net_target"),
                    "stop": state.get("plans", {}).get(side, {}).get("net_stop"),
                    "holding_seconds": state.get("plans", {}).get(side, {}).get("holding_seconds"),
                    "manual_delay_seconds": state.get("model", {})
                    .get("execution_assumptions", {})
                    .get("manual_delay_seconds"),
                    "slippage_per_leg": state.get("model", {})
                    .get("execution_assumptions", {})
                    .get("slippage_per_leg"),
                    "fee_type": state.get("market", {}).get("fee_type"),
                    "fee_multiplier": state.get("market", {}).get("fee_multiplier"),
                },
                **{
                    k: detail.get(k)
                    for k in (
                        "target_probability_given_entry",
                        "net_expected_profit_per_opportunity",
                        "net_ev_sampling_interval",
                        "entered_paths",
                        "entry_cap",
                    )
                },
            }
        )
    return candidates


def record(db, state, recorded_at):
    """Capture only current evidence before outcomes; never a paper fill."""
    market = state.get("market") or {}
    generated = state.get("generated_at") or state.get("prediction_engine", {}).get("updated_at")
    try:
        at = recorded_at if isinstance(recorded_at, datetime) else timestamp(recorded_at)
        generated_at, observed = timestamp(generated), timestamp(market["observed_at"])
        if not observed <= generated_at <= at or not market["ticker"].startswith("KXBTC15M-"):
            return False
        payload = {
            "timestamp": state.get("now"),
            "generated_at": generated_at.isoformat(),
            "data_cutoff": state.get("prediction_engine", {}).get("data_cutoff")
            or state.get("now"),
            "recorded_at": at.isoformat(),
            "mode": state.get("mode"),
            "candidate_data_stale": bool(state.get("stale", True)),
            "market": {
                k: market.get(k)
                for k in (
                    "ticker",
                    "observed_at",
                    "quote_event_time",
                    "quote_source",
                    "opens_at",
                    "closes_at",
                    "fee_type",
                    "fee_multiplier",
                    "quotes",
                    "book_diagnostic",
                )
            },
            "candidates": _candidate(state),
            "scalp_call": state.get("scalp"),
            "scalp_explanation": state.get("scalp_explanation"),
            "scanner": {
                k: state.get("model", {}).get("scanner", {}).get(k)
                for k in ("high_slippage_risk", "macro_blackout")
            },
            "regime": state.get("model", {}).get("analysis_24h", {}).get("regime", "unavailable"),
            "real_fill": False,
            "protocol": VERSION,
        }
        db.execute(
            "INSERT OR IGNORE INTO scalp_book_observations(mode,ticker,observed_at,generated_at,recorded_at,payload) VALUES (?,?,?,?,?,?)",
            (
                state.get("mode"),
                market["ticker"],
                observed.isoformat(),
                generated_at.isoformat(),
                at.isoformat(),
                json.dumps(payload, allow_nan=False, separators=(",", ":")),
            ),
        )
        return True
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _verified_legacy(db, mode, limit):
    """Explicit, bounded join to an independent actual recording timestamp."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"predictions", "btc_engine_predictions"} <= tables:
        return [], "Independent engine recording timestamps are unavailable"
    rows = db.execute(
        """SELECT p.timestamp,e.timestamp,e.recorded_at,
        json_extract(p.payload,'$.market'),json_extract(p.payload,'$.model.scalp_edges'),
        json_extract(p.payload,'$.model.scanner.high_slippage_risk'),
        json_extract(p.payload,'$.model.scanner.macro_blackout'),
        json_extract(p.payload,'$.model.analysis_24h.regime'),
        json_extract(p.payload,'$.plans'),json_extract(p.payload,'$.model.execution_assumptions'),
        json_extract(p.payload,'$.stale')
        FROM (SELECT id,mode,ticker,timestamp,payload FROM predictions WHERE mode=? ORDER BY id DESC LIMIT ?) p
        JOIN btc_engine_predictions e ON e.mode=p.mode AND e.ticker=p.ticker
        AND e.timestamp=json_extract(p.payload,'$.prediction_engine.updated_at')
        ORDER BY e.recorded_at,p.id""",
        (mode, min(limit, 1000)),
    ).fetchall()
    records = []
    for raw in rows:
        try:
            market = json.loads(raw[3])
            edge_state = {
                "model": {
                    "scalp_edges": json.loads(raw[4]) if raw[4] else {},
                    "execution_assumptions": json.loads(raw[9]) if raw[9] else {},
                },
                "plans": json.loads(raw[8]) if raw[8] else {},
                "market": market,
            }
            records.append(
                {
                    "mode": mode,
                    "timestamp": raw[0],
                    "data_cutoff": raw[0],
                    "generated_at": raw[1],
                    "recorded_at": raw[2],
                    "market": market,
                    "candidates": _candidate(edge_state),
                    "candidate_data_stale": bool(raw[10]) if raw[10] is not None else True,
                    "scanner": {"high_slippage_risk": bool(raw[5]), "macro_blackout": bool(raw[6])},
                    "regime": raw[7] or "unavailable",
                    "provenance": "Bounded legacy row joined by exact model generation timestamp to independent engine recorded_at",
                }
            )
        except (ValueError, TypeError):
            continue
    return (
        records,
        "At most 1000 recent legacy rows scanned; only exact independent generation/recording matches are eligible",
    )


def load_journal(path, *, limit=10000, mode="PUBLIC MARKET DATA", include_verified_legacy=False):
    """Bounded, read-only normalized records; never load legacy payload blobs."""
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError("Record limit must be 1..100000")
    if mode not in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
        raise ValueError("Public and synthetic modes must be evaluated separately")

    def available_time(value):
        try:
            return timestamp(value.get("available_at", value.get("recorded_at")))
        except (ValueError, TypeError, AttributeError):
            # Keep malformed timing visible to evaluate's exclusion counters.
            return datetime.min.replace(tzinfo=timezone.utc)

    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scalp_book_observations'"
        ).fetchone()
        if not exists and not include_verified_legacy:
            return {
                "records": [],
                "coverage": {
                    "status": "INSUFFICIENT_HISTORICAL_ORDER_BOOKS",
                    "loaded_records": 0,
                    "reason": "No normalized pre-outcome book journal. Legacy prediction rows do not prove actual generation/recording times; collect forward observations instead.",
                },
            }
        rows = (
            db.execute(
                "SELECT payload FROM (SELECT id,payload FROM scalp_book_observations WHERE mode=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (mode, limit + 1),
            ).fetchall()
            if exists
            else []
        )
        truncated = len(rows) > limit
        rows = rows[-limit:]
        records, malformed = [], 0
        for row in rows:
            try:
                parsed = json.loads(row[0])
                if not isinstance(parsed, dict):
                    raise ValueError("Book journal record must be an object")
                records.append(parsed)
            except (ValueError, TypeError):
                malformed += 1
        legacy_count, legacy_reason = 0, "Not scanned; forward evidence is the default"
        if include_verified_legacy:
            legacy, legacy_reason = _verified_legacy(db, mode, limit)
            legacy_count = len(legacy)
            records.extend(legacy)
            records.sort(key=available_time)
        official_count = 0
        has_outcomes = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forecast_outcomes'"
        ).fetchone()
        if has_outcomes and mode == "PUBLIC MARKET DATA":
            contracts = {
                r["market"]["ticker"]: r["market"]["closes_at"]
                for r in records
                if isinstance(r, dict)
                and isinstance(r.get("market"), dict)
                and r["market"].get("ticker")
                and r["market"].get("closes_at")
            }
            for ticker, closes_at in contracts.items():
                outcome = db.execute(
                    "SELECT outcome,observed_at FROM forecast_outcomes WHERE ticker=? AND origin='official'",
                    (ticker,),
                ).fetchone()
                if outcome is None:
                    continue
                try:
                    if (
                        not timestamp(closes_at)
                        <= timestamp(outcome[1])
                        <= datetime.now(timezone.utc)
                    ):
                        continue
                except (ValueError, TypeError, AttributeError):
                    continue
                if outcome[0] not in (0, 1):
                    continue
                records.append(
                    {
                        "type": "official_settlement",
                        "mode": mode,
                        "ticker": ticker,
                        "closes_at": closes_at,
                        "outcome": "up" if outcome[0] else "down",
                        "origin": "official",
                        "available_at": outcome[1],
                        "provenance": "Stored official result observation time, not an invented executable quote",
                    }
                )
                official_count += 1
        if official_count:
            records.sort(key=available_time)
        # The limit applies to the combined chronological replay, including
        # explicit legacy opt-in and settlement observations. Do not exceed
        # evaluate's memory bound when both journals contain a full window.
        if len(records) > limit:
            records, truncated = records[-limit:], True
    return {
        "records": records,
        "coverage": {
            "status": "OBSERVED_FORWARD_BOOKS"
            if records
            else "INSUFFICIENT_HISTORICAL_ORDER_BOOKS",
            "mode": mode,
            "loaded_records": len(records),
            "malformed_records": malformed,
            "truncated": truncated,
            "verified_legacy_joined_records": legacy_count,
            "official_result_observations": official_count,
            "legacy_selection": legacy_reason,
            "record_limit": limit,
            "reason": "Latest bounded observation window only; earlier paper positions are not reconstructed across a truncated boundary"
            if truncated
            else "Timestamped observation replay; displayed depth does not establish real fills",
        },
    }


def _normalize(raw, criteria):
    generated = timestamp(raw["generated_at"])
    at = timestamp(raw["recorded_at"])
    market = raw["market"]
    if raw.get("mode") not in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
        raise ValueError("UNSUPPORTED_DATA_MODE")
    observed = timestamp(market["observed_at"])
    closes = timestamp(market["closes_at"])
    opened = (
        timestamp(market["opens_at"]) if market.get("opens_at") else closes - timedelta(minutes=15)
    )
    if not observed <= generated <= at:
        raise ValueError("UNVERIFIED_OR_FUTURE_GENERATION_TIMING")
    cutoff = timestamp(raw["data_cutoff"]) if raw.get("data_cutoff") else None
    if cutoff is not None and cutoff > generated:
        raise ValueError("FUTURE_PREDICTION_DATA_CUTOFF")
    if not 0 <= (at - observed).total_seconds() <= criteria.maximum_quote_age_seconds:
        raise ValueError("STALE_OR_FUTURE_BOOK")
    if not opened <= observed <= generated <= at < closes:
        raise ValueError("INACTIVE_CONTRACT_BOOK")
    if market.get("book_diagnostic"):
        raise ValueError("EXECUTABLE_BOOK_UNAVAILABLE")
    if market.get("fee_type") not in FEES:
        raise ValueError("UNKNOWN_FEES")
    multiplier = number(market["fee_multiplier"], 0, 100)
    ticker = market["ticker"]
    if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
        raise ValueError("UNSUPPORTED_CONTRACT")
    quotes = {}
    for side in ("up", "down"):
        q = market["quotes"][side]
        bid = number(q["bid"], 0, 1)
        ask = number(q["ask"], bid, 1)
        quotes[side] = dict(
            bid=bid,
            ask=ask,
            bid_size=number(q["bid_size"], 0, 1e12),
            ask_size=number(q["ask_size"], 0, 1e12),
        )
    if (
        quotes["up"]["bid"] + quotes["down"]["bid"] > 1.0000001
        or quotes["up"]["ask"] + quotes["down"]["ask"] < 0.9999999
    ):
        raise ValueError("INCOMPATIBLE_BINARY_BOOK")
    return {
        **raw,
        "at": at,
        "generated": generated,
        "observed": observed,
        "closes": closes,
        "ticker": ticker,
        "quotes": quotes,
        "multiplier": multiplier,
        "forecast_cutoff_verified": cutoff is not None,
    }


def _chosen(row, criteria):
    if not row["forecast_cutoff_verified"] or row.get("candidate_data_stale"):
        return None
    options = []
    scanner = row.get("scanner") or {}
    if scanner.get("macro_blackout") or scanner.get("high_slippage_risk"):
        return None
    for item in row.get("candidates", []):
        try:
            side = item["side"]
            if side not in ("up", "down"):
                continue
            definition = item["model_definition"]
            if (
                definition.get("fee_type") != row["market"]["fee_type"]
                or number(definition.get("fee_multiplier"), 0, 100) != row["multiplier"]
            ):
                continue
            if any(
                abs(number(definition[name], 0, 10000) - getattr(criteria, name)) > 1e-8
                for name in (
                    "quantity",
                    "target",
                    "stop",
                    "holding_seconds",
                    "manual_delay_seconds",
                    "slippage_per_leg",
                )
            ):
                continue
            probability = number(item["target_probability_given_entry"], 0, 1)
            ev = number(item["net_expected_profit_per_opportunity"], -1, 1)
            lower = number(item["net_ev_sampling_interval"][0], -1, 1)
            if (
                probability >= criteria.minimum_target_probability
                and ev > 0
                and lower > 0
                and item["entered_paths"] >= criteria.minimum_entered_paths
            ):
                options.append((ev, side))
        except (KeyError, ValueError, TypeError, IndexError):
            continue
    options.sort(reverse=True)
    return (
        options[0][1] if options and (len(options) == 1 or options[0][0] > options[1][0]) else None
    )


class _Strategy:
    def __init__(self, name, criteria):
        self.name, self.criteria = name, criteria
        self.active, self.completed, self.attempts = [], [], []
        self.last_opportunity, self.capacity = {}, {}
        self.opportunities = self.blocked = self.paper_fills = 0

    def _capacity(self, row, side, leg):
        quote = row["quotes"][side]
        key = (row["ticker"], side, leg, quote[leg])
        reported = quote[leg + "_size"]
        if key not in self.capacity:
            self.capacity[key] = {
                "available": reported,
                "reported": reported,
                "at": row["observed"],
            }
        current = self.capacity[key]
        if row["observed"] > current["at"]:
            change = reported - current["reported"]
            current["available"] = (
                current["available"] + change if change > 0 else min(current["available"], reported)
            )
            current.update(reported=reported, at=row["observed"])
        return key, current["available"]

    def _finish(self, trade, status):
        if (
            status == "SIMULATED_TARGET"
            and trade["cashflow"] < trade["quantity"] * self.criteria.target - 1e-9
        ):
            status = "SIMULATED_TARGET_EXIT_SHORTFALL"
        trade["status"] = "UNOBSERVED_PATH" if trade["uncertain"] else status
        trade["net_pnl"] = trade["cashflow"] if not trade["uncertain"] else None
        trade["observed_cashflow_net_pnl"] = trade["cashflow"]
        self.completed.append(trade)
        self.active.remove(trade)

    def update(self, row):
        c, at = self.criteria, row["at"]
        for trade in list(self.active):
            if trade["entry_remaining"] > 0 and at > trade["entry_deadline"]:
                trade["entry_remaining"] = 0
                if trade["quantity"] == 0:
                    trade["status"] = "MISSED_FILL"
                    self.active.remove(trade)
                    continue
            if trade["ticker"] != row["ticker"]:
                continue
            q = row["quotes"][trade["side"]]
            if (
                trade["quantity"] > trade["exited_quantity"]
                and trade["last_quote_at"]
                and (row["observed"] - trade["last_quote_at"]).total_seconds()
                > c.maximum_gap_seconds
            ):
                trade["uncertain"] = True
            if trade["last_quote_at"] is None or row["observed"] > trade["last_quote_at"]:
                trade["last_quote_at"] = row["observed"]
            remaining = trade["quantity"] - trade["exited_quantity"]
            if remaining and row["observed"] > trade["last_fill_quote"]:
                price = max(0, q["bid"] - c.slippage_per_leg)
                # Known remaining cost includes proportional entry fees.
                cost = trade["entry_cost"] * remaining / trade["quantity"]
                net = remaining * price - cost - cost_fee(price, remaining, row["multiplier"])
                unit = net / remaining
                key, available = self._capacity(row, trade["side"], "bid")
                reason = trade.get("exit_reason")
                deadline = trade["first_fill_at"] + timedelta(seconds=c.holding_seconds)
                if reason is None:
                    if at >= deadline and row["observed"] >= deadline:
                        reason = "TIME_LIMIT"
                    elif unit <= -c.stop + 1e-10:
                        reason = "STOP"
                    elif at < deadline and unit >= c.target - 1e-10 and available >= remaining:
                        reason = "TARGET"
                if reason is not None:
                    trade["exit_reason"] = reason
                    trade["entry_remaining"] = 0  # Any unfilled remainder was never invested.
                    quantity = min(remaining, available)
                    if quantity > 0:
                        fee_total = cost_fee(price, quantity, row["multiplier"])
                        trade["cashflow"] += quantity * price - fee_total
                        trade["exit_fees"] += fee_total
                        trade["exited_quantity"] += quantity
                        trade["exit_at"] = at
                        trade["exit_fills"].append(
                            {
                                "at": at.isoformat(),
                                "observed_at": row["observed"].isoformat(),
                                "price": price,
                                "observed_bid": q["bid"],
                                "slippage_allowance": c.slippage_per_leg,
                                "quantity": quantity,
                                "fee": fee_total,
                            }
                        )
                        self.capacity[key]["available"] -= quantity
                        self.paper_fills += 1
                        if trade["exited_quantity"] >= trade["quantity"] - 1e-9:
                            self._finish(trade, "SIMULATED_" + reason)
                            continue
            if trade in self.active and trade["entry_remaining"] > 0:
                self._enter(trade, row)
        side = _chosen(row, c) if self.name == "candidate" else "up"
        if side is None:
            return
        key = row["ticker"]
        last = self.last_opportunity.get(key)
        if last is not None and (at - last).total_seconds() < c.opportunity_interval_seconds:
            return
        self.last_opportunity[key] = at
        self.opportunities += 1
        q = row["quotes"][side]
        if (
            q["ask"] - q["bid"] > c.maximum_spread + 1e-10
            or q["ask_size"] <= 0
            or q["bid_size"] <= 0
        ):
            self.blocked += 1
            return
        if (
            row["closes"] - at
        ).total_seconds() <= c.manual_delay_seconds + c.entry_window_seconds + c.holding_seconds:
            self.blocked += 1
            return
        exposure = sum(
            t["quantity"] - t["exited_quantity"] + t["entry_remaining"] for t in self.active
        )
        if (
            len(self.active) >= c.max_open_positions
            or exposure + c.quantity > c.max_position_quantity
        ):
            self.blocked += 1
            return
        cap = q["ask"] + c.slippage_per_leg
        if not 0 < cap < 1:
            self.blocked += 1
            return
        trade = {
            "ticker": row["ticker"],
            "side": side,
            "signal_at": row["generated"],
            "available_at": at,
            "eligible_at": at + timedelta(seconds=c.manual_delay_seconds),
            "entry_deadline": at
            + timedelta(seconds=c.manual_delay_seconds + c.entry_window_seconds),
            "entry_cap": cap,
            "entry_remaining": c.quantity,
            "quantity": 0.0,
            "exited_quantity": 0.0,
            "entry_cost": 0.0,
            "entry_fees": 0.0,
            "exit_fees": 0.0,
            "cashflow": 0.0,
            "first_fill_at": None,
            "last_fill_quote": row["observed"],
            "last_quote_at": row["observed"],
            "regime": row.get("regime", "unavailable"),
            "status": "AWAITING_ENTRY",
            "uncertain": False,
            "entry_fills": [],
            "exit_fills": [],
            "real_fill": False,
        }
        self.active.append(trade)
        self.attempts.append(trade)
        if c.manual_delay_seconds == 0:
            self._enter(trade, row)

    def settle(self, event):
        """An official payout closes exposure, never proves scalp barriers."""
        for trade in list(self.active):
            if trade["ticker"] != event["ticker"]:
                continue
            trade["entry_remaining"] = 0
            if trade["quantity"] == 0:
                trade["status"] = "MISSED_FILL"
                self.active.remove(trade)
                continue
            remaining = trade["quantity"] - trade["exited_quantity"]
            payout = 1.0 if trade["side"] == event["outcome"] else 0.0
            trade["cashflow"] += remaining * payout
            trade["exited_quantity"] += remaining
            trade["exit_at"] = event["at"]
            trade["uncertain"] = True
            trade["exit_reason"] = "OFFICIAL_SETTLEMENT_ONLY"
            trade["official_settlement"] = {
                "outcome": event["outcome"],
                "available_at": event["at"].isoformat(),
                "payout_per_contract": payout,
                "quantity": remaining,
                "notice": "Official payout, not an executable exit quote or proof of scalp success; assumes no redemption fee",
            }
            self._finish(trade, "SETTLEMENT_WITH_UNOBSERVED_SCALP_PATH")

    def _enter(self, trade, row):
        c, at = self.criteria, row["at"]
        if at > trade["entry_deadline"]:
            trade["entry_remaining"] = 0
            if trade["quantity"] == 0:
                trade["status"] = "MISSED_FILL"
                self.active.remove(trade)
            return
        if (
            at < trade["eligible_at"]
            or row["observed"] < trade["eligible_at"]
            or trade.get("exit_reason")
        ):
            return
        quote = row["quotes"][trade["side"]]
        price = quote["ask"] + c.slippage_per_leg
        if price > trade["entry_cap"] + 1e-10 or not 0 < price < 1:
            return
        key, available = self._capacity(row, trade["side"], "ask")
        quantity = min(trade["entry_remaining"], available)
        invested = sum(
            t["entry_cost"] * (t["quantity"] - t["exited_quantity"]) / t["quantity"]
            for t in self.active
            if t["quantity"] > 0
        )
        budget = max(0, c.max_total_cost - invested)
        if quantity <= 0:
            return
        if quantity * price + cost_fee(price, quantity, row["multiplier"]) > budget:
            # Cost is monotonic; choose an explicitly fractional partial quantity.
            low, high = 0.0, quantity
            for _ in range(40):
                mid = (low + high) / 2
                if mid * price + cost_fee(price, mid, row["multiplier"]) <= budget:
                    low = mid
                else:
                    high = mid
            quantity = math.floor(low * 100) / 100
        if quantity < 0.01:
            return
        fee_total = cost_fee(price, quantity, row["multiplier"])
        cost = quantity * price + fee_total
        trade["quantity"] += quantity
        trade["entry_remaining"] = max(0, trade["entry_remaining"] - quantity)
        trade["entry_cost"] += cost
        trade["entry_fees"] += fee_total
        trade["cashflow"] -= cost
        trade["first_fill_at"] = trade["first_fill_at"] or at
        trade["last_fill_quote"] = row["observed"]
        trade["status"] = "OPEN" if trade["entry_remaining"] <= 1e-9 else "PARTIALLY_FILLED"
        trade["entry_fills"].append(
            {
                "at": at.isoformat(),
                "observed_at": row["observed"].isoformat(),
                "price": price,
                "observed_ask": quote["ask"],
                "slippage_allowance": c.slippage_per_leg,
                "quantity": quantity,
                "fee": fee_total,
            }
        )
        self.capacity[key]["available"] -= quantity
        self.paper_fills += 1

    def result(self):
        observed = [t for t in self.completed if t["net_pnl"] is not None]
        pnl = [t["net_pnl"] for t in observed]
        wins = sum(v > 0 for v in pnl)
        curve = peak = drawdown = 0.0
        for trade in sorted(observed, key=lambda t: t["exit_at"]):
            curve += trade["net_pnl"]
            peak = max(peak, curve)
            drawdown = max(drawdown, peak - curve)
        mean = sum(pnl) / len(pnl) if pnl else None
        half = (
            1.96 * math.sqrt(sum((v - mean) ** 2 for v in pnl) / (len(pnl) - 1) / len(pnl))
            if len(pnl) > 1
            else None
        )
        finished = len(self.completed)
        metrics = {
            "opportunities": self.opportunities,
            "opportunity_scope": "Qualified model events before execution/risk filters"
            if self.name == "candidate"
            else "First valid book per contract and configured opportunity interval",
            "blocked_opportunities": self.blocked,
            "simulated_trades": sum(t["quantity"] > 0 for t in self.attempts),
            "simulated_fill_events": self.paper_fills,
            "partial_entry_attempts": sum(
                0 < t["quantity"] < self.criteria.quantity - 1e-9 for t in self.attempts
            ),
            "missed_fills": sum(t["status"] == "MISSED_FILL" for t in self.attempts),
            "completed_trades": finished,
            "observed_complete_trades": len(observed),
            "unobserved_outcomes": finished - len(observed),
            "open_paper_positions": sum(t["quantity"] > t["exited_quantity"] for t in self.active),
            "pending_entry_attempts": sum(t["quantity"] == 0 for t in self.active),
            "remaining_open_quantity": sum(
                t["quantity"] - t["exited_quantity"] for t in self.active
            ),
            "remaining_open_cost_at_risk": sum(
                t["entry_cost"] * (t["quantity"] - t["exited_quantity"]) / t["quantity"]
                for t in self.active
                if t["quantity"]
            ),
            "win_rate_observed_only": wins / len(pnl) if pnl else None,
            "win_rate_interval": wilson_interval(wins, len(pnl)),
            "target_before_stop_or_time_rate_observed_only": sum(
                t["status"] == "SIMULATED_TARGET" for t in observed
            )
            / len(observed)
            if observed
            else None,
            "net_simulated_pnl": sum(pnl),
            "maximum_drawdown": drawdown,
            "average_net_return_per_observed_trade": mean,
            "mean_net_pnl_approximate_interval": [mean - half, mean + half]
            if half is not None
            else None,
            "by_regime": {
                regime: {
                    "observed_trades": len(rows),
                    "net_simulated_pnl": sum(t["net_pnl"] for t in rows),
                }
                for regime in sorted({t["regime"] for t in observed})
                for rows in [[t for t in observed if t["regime"] == regime]]
            },
            "trades": [_json_trade(t) for t in (self.completed + self.active)[-200:]],
            "trade_details_limit": 200,
            "real_trades": 0,
            "drawdown_scope": "Observed closed trades only; open/unobserved positions are reported separately, not marked with invented exit prices",
        }
        return metrics


def _json_trade(trade):
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in trade.items()}


def evaluate(records, criteria=None):
    c = PaperCriteria.parse(criteria)
    normalized, excluded, fingerprints, contradictory = [], Counter(), {}, set()
    seen_records = set()
    last_observed = {}
    last_at = None
    for count, raw in enumerate(records, 1):
        if count > 100000:
            raise ValueError("Replay exceeds bounded 100000-record window")
        try:
            if raw.get("type") == "official_settlement":
                at, closed = timestamp(raw["available_at"]), timestamp(raw["closes_at"])
                if (
                    raw.get("origin") != "official"
                    or raw.get("outcome") not in ("up", "down")
                    or at < closed
                ):
                    raise ValueError("UNVERIFIED_SETTLEMENT_RESULT")
                if last_at is not None and at < last_at:
                    raise ValueError("OUT_OF_ORDER_AVAILABILITY")
                if raw.get("mode") != "PUBLIC MARKET DATA":
                    raise ValueError("UNSUPPORTED_SETTLEMENT_MODE")
                ticker = raw.get("ticker")
                if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-"):
                    raise ValueError("UNSUPPORTED_SETTLEMENT_CONTRACT")
                key = (ticker, "official_settlement")
                if key in fingerprints and fingerprints[key] != raw["outcome"]:
                    contradictory.add(ticker)
                fingerprints[key] = raw["outcome"]
                last_at = at
                normalized.append({**raw, "at": at, "kind": "official_settlement"})
                continue
            row = _normalize(raw, c)
            if last_at is not None and row["at"] < last_at:
                raise ValueError("OUT_OF_ORDER_AVAILABILITY")
            last_at = row["at"]
            previous_observed = last_observed.get(row["ticker"])
            if previous_observed is not None and row["observed"] < previous_observed:
                raise ValueError("OUT_OF_ORDER_BOOK_RECEIPT")
            last_observed[row["ticker"]] = row["observed"]
            key = (row["ticker"], row["observed"].isoformat())
            fingerprint = json.dumps(row["quotes"], sort_keys=True)
            if key in fingerprints and fingerprints[key] != fingerprint:
                contradictory.add(row["ticker"])
            fingerprints[key] = fingerprint
            identity = (key, row["generated"].isoformat(), row["at"].isoformat())
            if identity in seen_records:
                excluded["DUPLICATE_RECORD"] += 1
                continue
            seen_records.add(identity)
            normalized.append(row)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "MISSING_REQUIRED_BOOK_FIELD"
            excluded[reason] += 1
    accepted = [r for r in normalized if r["ticker"] not in contradictory]
    modes = {r["mode"] for r in accepted}
    if len(modes) > 1:
        raise ValueError(
            "Live public observations and synthetic replay must be evaluated separately"
        )
    excluded["CONTRADICTORY_BOOK_CONTRACT"] += len(normalized) - len(accepted)
    candidate, baseline = _Strategy("candidate", c), _Strategy("simple_always_up", c)
    for row in accepted:
        if row.get("kind") == "official_settlement":
            candidate.settle(row)
            baseline.settle(row)
        else:
            candidate.update(row)
            baseline.update(row)
    books = [r for r in accepted if r.get("kind") != "official_settlement"]

    def window_key(row):
        bucket = (
            math.floor(row["at"].timestamp() / c.opportunity_interval_seconds)
            if c.opportunity_interval_seconds
            else row["at"].isoformat()
        )
        return row["ticker"], bucket

    windows = {window_key(r) for r in books}
    qualified_windows = {window_key(r) for r in books if _chosen(r, c) is not None}
    result = {
        "protocol": VERSION,
        "status": "PAPER_REPLAY_ONLY" if len(books) >= 2 else "INSUFFICIENT_HISTORICAL_ORDER_BOOKS",
        "criteria": asdict(c),
        "coverage": {
            "accepted_book_records": len(books),
            "official_settlement_events": len(accepted) - len(books),
            "evaluated_contract_time_windows": len(windows),
            "model_candidate_windows": len(qualified_windows),
            "model_wait_windows": len(windows - qualified_windows),
            "model_candidate_window_fraction": len(qualified_windows) / len(windows)
            if windows
            else None,
            "window_scope": "Observed contract/UTC interval windows; model candidate presence before execution/risk filters, never an actionable call or a fill",
            "excluded": {k: v for k, v in excluded.items() if v},
            "first_available_at": accepted[0]["at"].isoformat() if accepted else None,
            "last_available_at": accepted[-1]["at"].isoformat() if accepted else None,
            "contracts": len({r["ticker"] for r in accepted}),
            "mode": next(iter(modes), None),
        },
        "strategies": {
            "candidate": candidate.result(),
            "simple_always_up": baseline.result(),
            "no_trade": {
                "simulated_trades": 0,
                "net_simulated_pnl": 0.0,
                "maximum_drawdown": 0.0,
                "win_rate_observed_only": None,
                "real_trades": 0,
            },
        },
        "profitable_edge_demonstrated": False,
        "edge_status": "edge unverified",
        "notice": "Paper assumptions only, never actual fills or account results. Positive results do not establish executable profitability.",
        "assumptions": [
            "Decisions are processed when actually recorded; entry requires a new observed quote at/after manual delay and price within the frozen cap.",
            "Candidate model quantity, target, stop, horizon, delay and slippage must match the replay definition; mismatched/unavailable probability events are withheld, not reused. Baselines may still be tested.",
            "Displayed top-level depth bounds each partial fill. Repeated unchanged receipts do not replenish consumed liquidity; only observed size increases add capacity. Queue priority/hidden liquidity are unknown.",
            "Entry and exit slippage allowances plus size-rounded taker fees are charged on every observed simulated partial fill.",
            "No interpolation of missing prices; unknown barrier intervals are excluded from supported P&L/win metrics. Open positions remain open without an observed exit.",
            "Explicit official results can close simulated settlement exposure, but cannot prove an unobserved scalp path; their cashflow is separate from supported strategy P&L.",
            "Each strategy is an independent counterfactual. Simple baseline buys UP once per eligible contract/minute; no-trade baseline stays flat.",
            "Approximate intervals assume independent samples and exclude model error; serial dependence, missing paths and repeated experiments can invalidate them.",
        ],
    }
    return result


def evaluate_journal(
    path, criteria=None, *, limit=10000, mode="PUBLIC MARKET DATA", include_verified_legacy=False
):
    source = load_journal(
        path, limit=limit, mode=mode, include_verified_legacy=include_verified_legacy
    )
    report = evaluate(source["records"], criteria)
    report["journal_coverage"] = source["coverage"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, default=Path("signals_monitor.db"))
    parser.add_argument("--output", type=Path, default=Path("signals_scalp_paper.report.json"))
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument(
        "--include-verified-legacy",
        action="store_true",
        help="Explicitly inspect at most 1000 recent legacy rows and require exact independent recording-time matches",
    )
    parser.add_argument(
        "--criteria", type=Path, help="Explicit JSON paper criteria; never broker risk settings"
    )
    parser.add_argument(
        "--mode", choices=["PUBLIC MARKET DATA", "REPLAY / SIMULATED"], default="PUBLIC MARKET DATA"
    )
    args = parser.parse_args()
    try:
        criteria = json.loads(args.criteria.read_text()) if args.criteria else None
        result = evaluate_journal(
            args.journal,
            criteria,
            limit=args.limit,
            mode=args.mode,
            include_verified_legacy=args.include_verified_legacy,
        )
        from .model_registry import atomic_json

        atomic_json(args.output, result)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(
        result["status"]
        + ": "
        + str(result["coverage"]["accepted_book_records"])
        + " timestamp-verified book records; edge unverified"
    )


if __name__ == "__main__":
    main()

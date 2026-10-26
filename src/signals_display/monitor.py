"""Credential-free public GET adapter, replay engine, and simulation journal.

No trading SDK, settings, environment loading, private API, or order operations.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from time import perf_counter
from urllib.parse import urlencode, quote

import httpx

from .model import Display, evaluate, fee, number, timestamp
from .edges import assess_edges
from .reliability import metrics, wilson

UTC = timezone.utc
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def utcnow():
    return datetime.now(UTC)


class PublicFeed:
    """Only fixed public market-data GETs; never reads trading credentials."""

    def __init__(self, get=None):
        self.get = get or httpx.get
        self.transport_diagnostics = []
        self.candle_cache = None
        self.candles_checked = None

    def _read(self, url):
        started = perf_counter()
        response = self.get(
            url, timeout=10, follow_redirects=False, headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        self.transport_diagnostics.append(
            {
                "endpoint": url.split("?")[0],
                "round_trip_ms": round((perf_counter() - started) * 1000, 2),
                "received_at": utcnow().isoformat(),
                "exchange_event_latency": "unknown; receipt timestamps only",
            }
        )
        self.transport_diagnostics = self.transport_diagnostics[-8:]
        return response.json()

    def candles(self, now):
        if self.candles_checked is not None and (now - self.candles_checked).total_seconds() < 60:
            return self.candle_cache or {}
        self.candles_checked = now
        url = (
            "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles?"
            + urlencode(
                {
                    "start": int(now.timestamp()) - 7200,
                    "end": int(now.timestamp()),
                    "granularity": "ONE_MINUTE",
                    "limit": 120,
                }
            )
        )
        try:
            data = self._read(url)
            self.candle_cache = {
                "candles": data["candles"],
                "source": "Coinbase BTC-USD public one-minute candles (proxy)",
                "received_at": utcnow().isoformat(),
            }
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return {
                "error": "MISSING_CANDLES: public one-minute candle endpoint unavailable",
                **(self.candle_cache or {}),
            }
        return self.candle_cache

    def resolved(self, ticker):
        data = self._read(KALSHI + "/markets/" + quote(ticker, safe=""))
        market = data.get("market", {})
        return market.get("result") if market.get("status") in {"settled", "finalized"} else None

    def poll(self):
        markets, cursor, seen = [], None, set()
        for _ in range(20):
            params = {"series_ticker": "KXBTC15M", "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self._read(KALSHI + "/markets?" + urlencode(params))
            markets.extend(page["markets"])
            cursor = page.get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Repeated public pagination cursor")
            seen.add(cursor)
        else:
            raise ValueError("Incomplete market discovery; refusing partial selection")
        series = self._read(KALSHI + "/series/KXBTC15M")["series"]
        now = utcnow()
        result = []
        sources = series.get("settlement_sources", [])
        source = "; ".join(str(item.get("name", "")) for item in sources) or "unavailable"
        for raw in markets:
            if raw.get("status", "open") not in {"open", "active"}:
                continue
            if raw.get("series_ticker", "KXBTC15M") != "KXBTC15M":
                continue
            if not str(raw.get("ticker", "")).startswith("KXBTC15M-"):
                continue
            # Never guess the mapping of YES to UP from a ticker or title.
            orientation = raw.get("strike_type") in {"greater", "greater_or_equal", "above"}
            close = timestamp(raw["close_time"])
            start = max(timestamp(raw["open_time"]), close - timedelta(minutes=15))

            def price(name):
                return number(float(raw[name + "_dollars"]), 0, 1)

            quotes = {}
            try:
                quotes = {
                    "up": {
                        "bid": price("yes_bid"),
                        "ask": price("yes_ask"),
                        "bid_size": 0,
                        "ask_size": 0,
                    },
                    "down": {
                        "bid": price("no_bid"),
                        "ask": price("no_ask"),
                        "bid_size": 0,
                        "ask_size": 0,
                    },
                }
            except (KeyError, ValueError, TypeError):
                pass
            result.append(
                dict(
                    ticker=raw["ticker"],
                    series="KXBTC15M",
                    status="open",
                    opens_at=start.isoformat(),
                    closes_at=close.isoformat(),
                    observed_at=now.isoformat(),
                    strike=raw.get("floor_strike"),
                    settlement_reference=source,
                    up_definition="YES according to published contract settlement rules"
                    if orientation
                    else "unknown",
                    rules=raw.get("rules_primary", ""),
                    quotes=quotes,
                    fee_type=series.get("fee_type"),
                    fee_multiplier=series.get("fee_multiplier"),
                    mapping_verified=orientation,
                    averaging_rule_verified=(
                        orientation
                        and "CF Benchmarks" in source
                        and str(raw.get("rules_primary", ""))
                        .lower()
                        .count("simple average of the sixty seconds")
                        == 2
                        and "at least" in str(raw.get("rules_primary", "")).lower()
                        and "brti" in str(raw.get("rules_primary", "")).lower()
                    ),
                )
            )
        selected = active_market({"markets": result}, now)
        if selected and selected["mapping_verified"]:
            selected[
                "book_diagnostic"
            ] = "MISSING_DEPTH: public orderbook has an empty side or unsupported schema"
            try:
                book = self._read(
                    KALSHI + "/markets/" + quote(selected["ticker"], safe="") + "/orderbook"
                )
                if "orderbook_fp" in book:
                    fixed = book["orderbook_fp"]
                    yes = [
                        (number(float(p), 0, 1), number(float(q), 0, 1e12))
                        for p, q in fixed.get("yes_dollars", [])
                    ]
                    no = [
                        (number(float(p), 0, 1), number(float(q), 0, 1e12))
                        for p, q in fixed.get("no_dollars", [])
                    ]
                else:
                    legacy = book.get("orderbook", {})
                    yes = [
                        (number(p, 0, 100) / 100, number(q, 0, 1e12))
                        for p, q in (legacy.get("yes") or [])
                    ]
                    no = [
                        (number(p, 0, 100) / 100, number(q, 0, 1e12))
                        for p, q in (legacy.get("no") or [])
                    ]
                yes = [level for level in yes if level[1] > 0]
                no = [level for level in no if level[1] > 0]
                if yes and no:
                    selected["book_diagnostic"] = None
                    y, n = max(yes), max(no)
                    selected["quotes"] = {
                        "up": dict(bid=y[0], ask=round(1 - n[0], 6), bid_size=y[1], ask_size=n[1]),
                        "down": dict(
                            bid=n[0], ask=round(1 - y[0], 6), bid_size=n[1], ask_size=y[1]
                        ),
                    }
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                selected["book_diagnostic"] = (
                    "MISSING_DEPTH: public orderbook unavailable ("
                    + type(exc).__name__
                    + (" HTTP " + str(status) if status else "")
                    + ")"
                )
                # Summary quotes remain visible, but they never imply known executable depth.
        reference = {
            "price": None,
            "source": "Coinbase BTC-USD spot (proxy, NOT settlement index)",
            "observed_at": None,
            "official": False,
        }
        warnings = [
            "Coinbase is a proxy, not the official settlement index. Model estimates are UNVALIDATED."
        ]
        try:
            spot = self._read(COINBASE)["data"]
            if spot.get("currency") != "USD" or spot.get("base") != "BTC":
                raise ValueError("unexpected reference currency")
            reference.update(
                price=number(float(spot["amount"]), 0.000001, 1e12),
                observed_at=utcnow().isoformat(),
            )
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            warnings.append(
                "Public reference proxy unavailable; official settlement feed not connected."
            )
        return {
            "markets": result,
            "reference": reference,
            "warnings": warnings,
            "mode": "PUBLIC MARKET DATA",
            "evidence": {},
            "btc_candles": self.candles(utcnow()),
            "transport_diagnostics": list(self.transport_diagnostics),
        }


class ReplayFeed:
    def __init__(self, path):
        self.frames = json.loads(Path(path).read_text())
        if not isinstance(self.frames, list) or not self.frames:
            raise ValueError("Replay must be a nonempty list of frames")
        times = [timestamp(frame["now"]) for frame in self.frames]
        if any(b <= a for a, b in zip(times, times[1:])):
            raise ValueError("Replay timestamps must increase")
        self.index = 0
        self.last_wall = utcnow()

    def poll(self):
        index = min(self.index, len(self.frames) - 1)
        frame = self.frames[index]
        now = timestamp(frame["now"])
        if self.index >= len(self.frames):
            now += utcnow() - self.last_wall
        else:
            self.last_wall = utcnow()
        self.index += 1
        return {**frame, "mode": "REPLAY / SIMULATED", "clock": now.isoformat()}


def active_market(payload, now):
    candidates = []
    for market in payload.get("markets", []):
        if market.get("series") != "KXBTC15M" or market.get("status") != "open":
            continue
        start, end = timestamp(market["opens_at"]), timestamp(market["closes_at"])
        if 0 < (end - start).total_seconds() <= 900 and start <= now < end:
            candidates.append(market)
    return candidates[0] if len(candidates) == 1 else None


def levels(entry, multiplier, target, stop):
    """Hypothetical exit bids for requested net thresholds, including both fees."""
    prices = [
        (cent / 100, cent / 100 - entry - fee(entry, multiplier) - fee(cent / 100, multiplier))
        for cent in range(101)
    ]
    targets = [bid for bid, pnl in prices if pnl >= target - 1e-10]
    stops = [bid for bid, pnl in prices if pnl <= -stop + 1e-10]
    return (min(targets) if targets else None, max(stops) if stops else None)


def analyze(payload, now, target=0.05, stop=0.03, horizon=60, model_result=None, model_info=None):
    market = active_market(payload, now)
    result = (
        model_result
        if model_result is not None
        else evaluate(payload, now=now, target=target, stop=stop, horizon=horizon)
    )
    if market and not market.get("mapping_verified", True):
        result = Display("WAIT", "YES/UP settlement mapping unverified")
    stale = True
    if market:
        age = (now - timestamp(market["observed_at"])).total_seconds()
        stale = not 0 <= age <= 5
    direction = "UNCLEAR"
    if result.settlement_up is not None and not stale:
        if result.settlement_up >= 0.6:
            direction = "UP"
        elif result.settlement_up <= 0.4:
            direction = "DOWN"
    plans = {}
    if market and not stale and market.get("mapping_verified", True):
        try:
            multiplier = number(market["fee_multiplier"], 0, 100)
            if market["fee_type"] not in {"quadratic", "quadratic_with_maker_fees"}:
                raise ValueError("unknown fees")
            for side in ("up", "down"):
                bid = number(market["quotes"][side]["bid"], 0, 1)
                entry = number(market["quotes"][side]["ask"], max(0.000001, bid), 0.999999)
                target_bid, stop_bid = levels(entry, multiplier, target, stop)
                plans[side] = dict(
                    entry=entry,
                    target_bid=target_bid,
                    stop_bid=stop_bid,
                    net_target=target,
                    net_stop=stop,
                    holding_seconds=horizon,
                    probability=getattr(result, "scalp_" + side),
                    multiplier=multiplier,
                    entry_fee=fee(entry, multiplier),
                )
        except (KeyError, ValueError, TypeError):
            plans = {}
    available = [
        (side, plan["probability"])
        for side, plan in plans.items()
        if plan["probability"] is not None
        and plan["probability"] >= 0.6
        and plan["target_bid"] is not None
    ]
    available.sort(key=lambda item: item[1], reverse=True)
    scalp = "WAIT"
    if (
        result.status != "WAIT"
        and available
        and (len(available) == 1 or available[0][1] > available[1][1])
    ):
        scalp = available[0][0].upper() + " SCALP"
    scalp_lean = (
        ("SCALP " + available[0][0].upper())
        if available and (len(available) == 1 or available[0][1] > available[1][1])
        else "WAIT"
    )
    edges = {}
    if model_info and model_info.get("enabled") and market and not stale:
        edges = assess_edges(market, result, model_info, now)
        scalp = edges["scalp_call"]
        for side, plan in plans.items():
            execution = model_info.get("execution_assumptions", {})
            slip = execution.get("slippage_per_leg", 0.01)
            delay = execution.get("manual_delay_seconds", 3)
            cap = plan["entry"] + slip
            if cap >= 1:
                continue
            plan.update(
                display_ask=plan["entry"],
                entry=cap,
                entry_fee=fee(cap, plan["multiplier"]),
                manual_delay=delay,
                slippage=slip,
                model_version=model_info.get("version"),
            )
            target_bid, stop_bid = levels(cap, plan["multiplier"], target, stop)
            plan["target_bid"] = (
                target_bid + slip if target_bid is not None and target_bid + slip <= 1 else None
            )
            plan["stop_bid"] = (
                stop_bid + slip if stop_bid is not None and stop_bid + slip <= 1 else None
            )
    overall_reason = (model_info or {}).get("overall_reason", result.reason)
    scalp_reason = (model_info or {}).get("scalp_reason", result.reason)
    if stale:
        overall_reason = (
            scalp_reason
        ) = "STALE_OR_MISSING_CONTRACT: fresh unique contract snapshot required"
    elif result.settlement_up is not None and direction == "UNCLEAR":
        overall_reason += "; UNCLEAR: neither direction meets the 60% decision threshold"
    if (
        not stale
        and result.scalp_up is not None
        and result.scalp_down is not None
        and scalp == "WAIT"
    ):
        scalp_reason += "; WAIT: no unique side at or above 60% with a feasible target"
    if edges:
        scalp_reason += "; " + (
            "; ".join(
                side.upper() + ": " + data["reason"]
                for side, data in edges.get("scalp", {}).items()
            )
            or "WAIT: cost-adjusted scalp estimates unavailable"
        )
    return dict(
        transport_diagnostics=payload.get("transport_diagnostics", []),
        edges=edges,
        scalp_lean=scalp_lean,
        settlement_qualification="WAIT" if edges else "UNVERIFIED",
        model=model_info or {"version": "uploaded-scenarios", "validated": False},
        now=now.isoformat(),
        mode=payload.get("mode", "REPLAY / SIMULATED"),
        market=market,
        reference=payload.get("reference", {}),
        stale=stale,
        remaining_seconds=max(0, (timestamp(market["closes_at"]) - now).total_seconds())
        if market
        else 0,
        direction=direction,
        scalp=scalp,
        estimate=asdict(result),
        plans=plans,
        warnings=payload.get("warnings", []),
        explanation=overall_reason,
        scalp_explanation=scalp_reason,
    )


class Journal:
    """Append-only predictions and explicitly counterfactual one-contract probes."""

    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY, mode TEXT, ticker TEXT, timestamp TEXT,
                    direction TEXT, scalp TEXT, probability_up REAL, probability_down REAL,
                    payload TEXT, settlement_up INTEGER, UNIQUE(mode,ticker,timestamp));
                CREATE TABLE IF NOT EXISTS simulations (
                    id INTEGER PRIMARY KEY, prediction_id INTEGER, mode TEXT, ticker TEXT,
                    side TEXT, started_at TEXT, last_at TEXT, deadline TEXT,
                    entry REAL, entry_fee REAL, multiplier REAL, target REAL, stop REAL,
                    last_bid REAL, status TEXT, exit_at TEXT, exit_bid REAL, exit_fee REAL,
                    net_pnl REAL, real_fill INTEGER NOT NULL DEFAULT 0 CHECK(real_fill=0),
                    probability REAL, UNIQUE(prediction_id,side));
            """
            )

            columns = {row[1] for row in db.execute("PRAGMA table_info(simulations)")}
            additions = {
                "eligible_at": "TEXT",
                "entry_cap": "REAL",
                "manual_delay": "INTEGER NOT NULL DEFAULT 0",
                "slippage": "REAL NOT NULL DEFAULT 0",
                "holding_seconds": "INTEGER",
                "model_version": "TEXT NOT NULL DEFAULT 'legacy-immediate-probe'",
                "execution_at": "TEXT",
                "call_at_prediction": "TEXT",
            }
            for name, spec in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE simulations ADD COLUMN {name} {spec}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS btc_candles (source TEXT,start INTEGER,payload TEXT,first_received_at TEXT,PRIMARY KEY(source,start))"
            )

    def record_candles(self, data, now):
        if not data or not data.get("source"):
            return
        with self.connect() as db:
            for bar in data.get("candles", []):
                try:
                    start = int(bar["start"])
                    if start + 60 > now.timestamp():
                        continue
                    db.execute(
                        "INSERT OR IGNORE INTO btc_candles VALUES (?,?,?,?)",
                        (data["source"], start, json.dumps(bar), data["received_at"]),
                    )
                except (KeyError, ValueError, TypeError):
                    continue

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def step(self, state):
        now = timestamp(state["now"])
        market = state["market"]
        with self.connect() as db:
            pending = db.execute(
                "SELECT * FROM simulations WHERE status IN ('PENDING','AWAITING_ENTRY')"
            ).fetchall()
            for probe in pending:
                last = timestamp(probe["last_at"])
                deadline = timestamp(probe["deadline"])
                same = market and market["ticker"] == probe["ticker"] and not state["stale"]
                bid = None
                if same:
                    side_quote = market.get("quotes", {}).get(probe["side"], {})
                    if side_quote.get("bid_size", 0) >= 1:
                        bid = side_quote.get("bid")
                status, pnl = None, None
                if now < last:
                    status = "UNOBSERVED_CLOCK_RESET"
                elif now == last:
                    continue
                elif same and (
                    market.get("fee_type") not in {"quadratic", "quadratic_with_maker_fees"}
                    or market.get("fee_multiplier") != probe["multiplier"]
                ):
                    status = "UNOBSERVED_FEE_CHANGE"
                elif now > deadline:
                    status = "UNOBSERVED_TIME_LIMIT"
                elif (now - last).total_seconds() > 5 or not same or bid is None:
                    status = "UNOBSERVED_DATA_GAP"
                elif probe["status"] == "AWAITING_ENTRY":
                    if now < timestamp(probe["eligible_at"]):
                        db.execute(
                            "UPDATE simulations SET last_at=? WHERE id=?",
                            (now.isoformat(), probe["id"]),
                        )
                        continue
                    ask = side_quote.get("ask")
                    if ask is None or side_quote.get("ask_size", 0) < 1:
                        status = "UNOBSERVED_ENTRY_DEPTH"
                    elif (
                        ask + probe["slippage"] > probe["entry_cap"] or ask + probe["slippage"] >= 1
                    ):
                        status = "SIMULATED_NO_ENTRY_PRICE_MOVED"
                    elif (timestamp(market["closes_at"]) - now).total_seconds() < probe[
                        "holding_seconds"
                    ]:
                        status = "SIMULATED_NO_ENTRY_TIME"
                    else:
                        entry = ask + probe["slippage"]
                        entry_fee = fee(entry, probe["multiplier"])
                        exit_bid = max(0, bid - probe["slippage"])
                        pnl = exit_bid - entry - entry_fee - fee(exit_bid, probe["multiplier"])
                        stopped = pnl <= -probe["stop"]
                        db.execute(
                            """UPDATE simulations SET entry=?,entry_fee=?,execution_at=?,last_at=?,deadline=?,status=?,
                            exit_at=?,exit_bid=?,exit_fee=?,net_pnl=? WHERE id=?""",
                            (
                                entry,
                                entry_fee,
                                now.isoformat(),
                                now.isoformat(),
                                (now + timedelta(seconds=probe["holding_seconds"])).isoformat(),
                                "SIMULATED_STOP" if stopped else "PENDING",
                                now.isoformat() if stopped else None,
                                exit_bid if stopped else None,
                                fee(exit_bid, probe["multiplier"]) if stopped else None,
                                pnl if stopped else None,
                                probe["id"],
                            ),
                        )
                        continue
                else:
                    bid = max(0, number(bid, 0, 1) - probe["slippage"])
                    pnl = bid - probe["entry"] - probe["entry_fee"] - fee(bid, probe["multiplier"])
                    if now >= deadline:
                        status = "SIMULATED_TIMEOUT"
                    elif pnl <= -probe["stop"]:
                        status = "SIMULATED_STOP"
                    elif pnl >= probe["target"]:
                        status = "SIMULATED_TARGET"
                if status:
                    observed = not status.startswith(("UNOBSERVED", "SIMULATED_NO_ENTRY"))
                    db.execute(
                        """UPDATE simulations SET status=?,exit_at=?,exit_bid=?,exit_fee=?,net_pnl=?
                                  WHERE id=?""",
                        (
                            status,
                            now.isoformat(),
                            bid if observed else None,
                            fee(bid, probe["multiplier"]) if observed else None,
                            pnl if observed else None,
                            probe["id"],
                        ),
                    )
                else:
                    db.execute(
                        "UPDATE simulations SET last_at=?,last_bid=? WHERE id=?",
                        (now.isoformat(), bid, probe["id"]),
                    )
            if not market or not market.get("mapping_verified", True):
                return
            e = state["estimate"]
            db.execute(
                """INSERT OR IGNORE INTO predictions
                (mode,ticker,timestamp,direction,scalp,probability_up,probability_down,payload)
                VALUES (?,?,?,?,?,?,?,?)""",
                (
                    state["mode"],
                    market["ticker"],
                    state["now"],
                    state["direction"],
                    state["scalp"],
                    e["settlement_up"],
                    e["settlement_down"],
                    json.dumps(state, allow_nan=False),
                ),
            )
            prediction = db.execute(
                "SELECT id FROM predictions WHERE mode=? AND ticker=? AND timestamp=?",
                (state["mode"], market["ticker"], state["now"]),
            ).fetchone()[0]
            if state["stale"]:
                return
            for side, plan in state["plans"].items():
                # One counterfactual probe per side/minute/settings, not a signal fill.
                recent = db.execute(
                    """SELECT started_at FROM simulations WHERE mode=? AND ticker=?
                    AND side=? AND target=? AND stop=?
                    AND COALESCE(holding_seconds,ROUND((julianday(deadline)-julianday(started_at))*86400))=?
                    AND manual_delay=? AND slippage=?
                    ORDER BY id DESC LIMIT 1""",
                    (
                        state["mode"],
                        market["ticker"],
                        side,
                        plan["net_target"],
                        plan["net_stop"],
                        plan["holding_seconds"],
                        plan.get("manual_delay", 0),
                        plan.get("slippage", 0),
                    ),
                ).fetchone()
                if recent and (now - timestamp(recent[0])).total_seconds() < 60:
                    continue
                if (
                    plan["holding_seconds"] + plan.get("manual_delay", 0)
                    > state["remaining_seconds"]
                    or plan["target_bid"] is None
                ):
                    continue
                quote = market["quotes"][side]
                if quote.get("ask_size", 0) < 1 or quote.get("bid_size", 0) < 1:
                    continue
                if plan.get("model_version"):
                    delay = plan.get("manual_delay", 0)
                    slipped_bid = max(0, quote["bid"] - plan["slippage"])
                    entry = plan["entry"] if delay == 0 else None
                    entry_fee = fee(entry, plan["multiplier"]) if entry is not None else None
                    net = (
                        slipped_bid - entry - entry_fee - fee(slipped_bid, plan["multiplier"])
                        if entry is not None
                        else None
                    )
                    stopped = net is not None and net <= -plan["net_stop"]
                    record = dict(
                        prediction_id=prediction,
                        mode=state["mode"],
                        ticker=market["ticker"],
                        side=side,
                        started_at=now.isoformat(),
                        last_at=now.isoformat(),
                        deadline=(
                            now + timedelta(seconds=delay + plan["holding_seconds"])
                        ).isoformat(),
                        multiplier=plan["multiplier"],
                        target=plan["net_target"],
                        stop=plan["net_stop"],
                        status="AWAITING_ENTRY"
                        if delay
                        else ("SIMULATED_STOP" if stopped else "PENDING"),
                        probability=plan["probability"],
                        eligible_at=(now + timedelta(seconds=delay)).isoformat(),
                        entry_cap=plan["entry"],
                        manual_delay=delay,
                        slippage=plan["slippage"],
                        holding_seconds=plan["holding_seconds"],
                        model_version=plan["model_version"],
                        call_at_prediction=state["scalp"],
                        entry=entry,
                        entry_fee=entry_fee,
                        execution_at=now.isoformat() if not delay else None,
                        exit_at=now.isoformat() if stopped else None,
                        exit_bid=slipped_bid if stopped else None,
                        exit_fee=fee(slipped_bid, plan["multiplier"]) if stopped else None,
                        net_pnl=net if stopped else None,
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO simulations ("
                        + ",".join(record)
                        + ") VALUES ("
                        + ",".join("?" for _ in record)
                        + ")",
                        list(record.values()),
                    )
                    continue
                pnl = (
                    quote["bid"]
                    - plan["entry"]
                    - plan["entry_fee"]
                    - fee(quote["bid"], plan["multiplier"])
                )
                immediate = pnl <= -plan["net_stop"]
                db.execute(
                    """INSERT OR IGNORE INTO simulations
                    (prediction_id,mode,ticker,side,started_at,last_at,deadline,entry,entry_fee,multiplier,
                     target,stop,last_bid,status,probability,exit_at,exit_bid,exit_fee,net_pnl)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        prediction,
                        state["mode"],
                        market["ticker"],
                        side,
                        now.isoformat(),
                        now.isoformat(),
                        (now + timedelta(seconds=plan["holding_seconds"])).isoformat(),
                        plan["entry"],
                        plan["entry_fee"],
                        plan["multiplier"],
                        plan["net_target"],
                        plan["net_stop"],
                        quote["bid"],
                        "SIMULATED_STOP" if immediate else "PENDING",
                        plan["probability"],
                        now.isoformat() if immediate else None,
                        quote["bid"] if immediate else None,
                        fee(quote["bid"], plan["multiplier"]) if immediate else None,
                        pnl if immediate else None,
                    ),
                )

    def settle(self, ticker, yes_result, mode):
        if yes_result not in {"yes", "no"}:
            return
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM predictions WHERE ticker=? AND mode=? AND settlement_up IS NULL",
                (ticker, mode),
            ).fetchall()
            for row in rows:
                if json.loads(row["payload"])["market"].get("mapping_verified", True):
                    db.execute(
                        "UPDATE predictions SET settlement_up=? WHERE id=?",
                        (int(yes_result == "yes"), row["id"]),
                    )

    def recent_states(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM predictions WHERE mode='PUBLIC MARKET DATA' ORDER BY id DESC LIMIT 600"
            ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def performance(self):
        with self.connect() as db:
            research = metrics(db)
            rows = db.execute(
                """SELECT mode,ticker,direction,probability_up,settlement_up,COALESCE(json_extract(payload,'$.model.version'),'uploaded-scenarios') version
                FROM predictions WHERE direction IN ('UP','DOWN')
                AND probability_up IS NOT NULL AND settlement_up IS NOT NULL
                ORDER BY timestamp,id"""
            ).fetchall()
        unique = {}
        for row in rows:
            unique.setdefault((row["mode"], row["ticker"]), row)
        result = {}
        for mode in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
            samples = [r for (m, _), r in unique.items() if m == mode]
            correct = sum((r["direction"] == "UP") == bool(r["settlement_up"]) for r in samples)
            result[mode] = dict(
                contracts=len(samples),
                correct=correct,
                accuracy_sampling_interval=wilson(correct, len(samples)) if samples else None,
                accuracy=correct / len(samples) if samples else None,
                brier_score=sum((r["probability_up"] - r["settlement_up"]) ** 2 for r in samples)
                / len(samples)
                if samples
                else None,
            )
        by_model = {}
        unique_model = {}
        for row in rows:
            unique_model.setdefault((row["mode"], row["version"], row["ticker"]), row)
        for (mode, version, _), row in unique_model.items():
            group = by_model.setdefault(
                mode + " / " + version, {"contracts": 0, "correct": 0, "brier_score": 0}
            )
            group["contracts"] += 1
            group["correct"] += int((row["direction"] == "UP") == bool(row["settlement_up"]))
            group["brier_score"] += (row["probability_up"] - row["settlement_up"]) ** 2
        for group in by_model.values():
            group["accuracy"] = group["correct"] / group["contracts"]
            group["brier_score"] /= group["contracts"]
            group["accuracy_interval"] = wilson(group["correct"], group["contracts"])
        research["settlement_by_model_and_mode"] = by_model
        return {
            "research": research,
            "accuracy_goal": 0.9,
            "validated": False,
            "by_mode": result,
            "method": "First supported directional prediction per settled contract; "
            "UNCLEAR excluded. Replay is synthetic, never live validation. "
            "90% is a measurement goal, not an accuracy claim.",
        }

    def history(self):
        with self.connect() as db:
            predictions = [
                dict(row)
                for row in db.execute(
                    "SELECT id,mode,ticker,timestamp,direction,scalp,probability_up,probability_down,settlement_up FROM predictions ORDER BY id DESC LIMIT 200"
                )
            ]
            simulations = [
                dict(row)
                for row in db.execute("SELECT * FROM simulations ORDER BY id DESC LIMIT 200")
            ]
        return {
            "performance": self.performance(),
            "predictions": predictions,
            "simulations": simulations,
            "notice": "COUNTERFACTUAL one-contract probes only — never real fills or trades. "
            "Polling can miss intrapoll barriers; unknown gaps are excluded, not wins.",
        }

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
from urllib.parse import urlencode, quote

import httpx

from .model import Display, evaluate, fee, number, timestamp

UTC = timezone.utc
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def utcnow():
    return datetime.now(UTC)


class PublicFeed:
    """Only fixed public market-data GETs; never reads trading credentials."""

    def __init__(self, get=None):
        self.get = get or httpx.get

    def _read(self, url):
        response = self.get(
            url, timeout=10, follow_redirects=False, headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        return response.json()

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
                )
            )
        selected = active_market({"markets": result}, now)
        if selected and selected["mapping_verified"]:
            try:
                book = self._read(
                    KALSHI + "/markets/" + quote(selected["ticker"], safe="") + "/orderbook"
                )
                if "orderbook_fp" in book:
                    fixed = book["orderbook_fp"]
                    yes = [
                        (number(float(p), 0, 1), number(float(q), 0.000001, 1e12))
                        for p, q in fixed.get("yes_dollars", [])
                    ]
                    no = [
                        (number(float(p), 0, 1), number(float(q), 0.000001, 1e12))
                        for p, q in fixed.get("no_dollars", [])
                    ]
                else:
                    legacy = book.get("orderbook", {})
                    yes = [
                        (number(p, 0, 100) / 100, number(q, 0.000001, 1e12))
                        for p, q in (legacy.get("yes") or [])
                    ]
                    no = [
                        (number(p, 0, 100) / 100, number(q, 0.000001, 1e12))
                        for p, q in (legacy.get("no") or [])
                    ]
                if yes and no:
                    y, n = max(yes), max(no)
                    selected["quotes"] = {
                        "up": dict(bid=y[0], ask=round(1 - n[0], 6), bid_size=y[1], ask_size=n[1]),
                        "down": dict(
                            bid=n[0], ask=round(1 - y[0], 6), bid_size=n[1], ask_size=y[1]
                        ),
                    }
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass  # Summary quotes remain visible; absent sizes prohibit simulations.
        reference = {
            "price": None,
            "source": "Coinbase BTC-USD spot (proxy, NOT settlement index)",
            "observed_at": None,
            "official": False,
        }
        warnings = [
            "Official settlement-index feed and calibrated conditional model are not connected; "
            "live probabilities remain unavailable."
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


def analyze(payload, now, target=0.05, stop=0.03, horizon=60):
    market = active_market(payload, now)
    result = evaluate(payload, now=now, target=target, stop=stop, horizon=horizon)
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
    return dict(
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
        explanation=(
            "Conditional scenarios only; UNVALIDATED. A 60% threshold selects direction."
            if result.settlement_up is not None
            else "No matched conditional settlement model. Quotes alone are not a settlement forecast."
        ),
        scalp_explanation=(
            "Net target before stop/deadline using hypothetical ask entry and bid exit. "
            "WAIT when evidence, fees, liquidity, or a unique probability leader is missing."
        ),
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

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def step(self, state):
        now = timestamp(state["now"])
        market = state["market"]
        with self.connect() as db:
            pending = db.execute("SELECT * FROM simulations WHERE status='PENDING'").fetchall()
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
                else:
                    bid = number(bid, 0, 1)
                    pnl = bid - probe["entry"] - probe["entry_fee"] - fee(bid, probe["multiplier"])
                    if pnl <= -probe["stop"]:
                        status = "SIMULATED_STOP"
                    elif now < deadline and pnl >= probe["target"]:
                        status = "SIMULATED_TARGET"
                    elif now >= deadline:
                        status = "SIMULATED_TIMEOUT"
                if status:
                    observed = not status.startswith("UNOBSERVED")
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
                    AND ROUND((julianday(deadline)-julianday(started_at))*86400)=?
                    ORDER BY id DESC LIMIT 1""",
                    (
                        state["mode"],
                        market["ticker"],
                        side,
                        plan["net_target"],
                        plan["net_stop"],
                        plan["holding_seconds"],
                    ),
                ).fetchone()
                if recent and (now - timestamp(recent[0])).total_seconds() < 60:
                    continue
                if (
                    plan["holding_seconds"] > state["remaining_seconds"]
                    or plan["target_bid"] is None
                ):
                    continue
                quote = market["quotes"][side]
                if quote.get("ask_size", 0) < 1 or quote.get("bid_size", 0) < 1:
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

    def performance(self):
        with self.connect() as db:
            rows = db.execute(
                """SELECT mode,ticker,direction,probability_up,settlement_up
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
                accuracy=correct / len(samples) if samples else None,
                brier_score=sum((r["probability_up"] - r["settlement_up"]) ** 2 for r in samples)
                / len(samples)
                if samples
                else None,
            )
        return {
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

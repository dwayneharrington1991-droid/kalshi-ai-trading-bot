"""Past-only 24h context and forward-recorded contract forecasts, not execution."""
from datetime import timedelta
import json
import math
from statistics import mean, pstdev, median
from .model import timestamp, number
from .scores import score

VERSION = "btc24h-final-average-v1"


def ema(values, period):
    value = values[0]
    alpha = 2 / (period + 1)
    result = []
    for x in values:
        value += alpha * (x - value)
        result.append(value)
    return result


def aggregate(bars, minutes):
    groups = {}
    for b in bars:
        groups.setdefault(b["start"] // (60 * minutes), []).append(b)
    out = []
    for key, group in sorted(groups.items()):
        if len(group) != minutes or group[0]["start"] != key * minutes * 60:
            continue
        out.append(
            dict(
                start=key * minutes * 60,
                open=group[0]["open"],
                high=max(x["high"] for x in group),
                low=min(x["low"] for x in group),
                close=group[-1]["close"],
                volume=sum(x["volume"] for x in group),
            )
        )
    return out


def context24(data, now):
    result = {
        "status": "INSUFFICIENT DATA",
        "reason": "Need 1440 contiguous closed one-minute candles",
        "source": data.get("source"),
        "features": {},
    }
    try:
        if not 0 <= (now - timestamp(data["received_at"])).total_seconds() <= 150:
            raise ValueError("Candle receipt stale/future-dated")
        available = [
            int(b["start"]) + 60
            for b in data["candles"]
            if int(b["start"]) + 60
            <= min(now.timestamp(), timestamp(data["received_at"]).timestamp())
        ]
        cutoff = max(available) if available else int(now.timestamp()) // 60 * 60
        if not 0 <= now.timestamp() - cutoff <= 120:
            raise ValueError("Latest complete historical minute is too old")
        bars = {}
        for raw in data["candles"]:
            start = int(raw["start"])
            if start < cutoff - 86400 or start + 60 > cutoff:
                continue
            if start % 60:
                raise ValueError("Unaligned candle")
            b = {
                k: number(float(raw[k]), 0 if k == "volume" else 0.000001, 1e15)
                for k in ("open", "high", "low", "close", "volume")
            }
            b["start"] = start
            if (
                not b["low"]
                <= min(b["open"], b["close"])
                <= max(b["open"], b["close"])
                <= b["high"]
            ):
                raise ValueError("Invalid OHLC")
            if start in bars:
                raise ValueError("Duplicate candle interval")
            bars[start] = b
        if sorted(bars) != list(range(cutoff - 86400, cutoff, 60)):
            raise ValueError(f"Incomplete 24h window: {len(bars)}/1440 closed bars")
        rows = [bars[t] for t in sorted(bars)]
        closes = [b["close"] for b in rows]
        returns = [b - a for a, b in zip(closes, closes[1:])]
        sigma = math.sqrt(sum(x * x for x in returns) / (60 * len(returns)))
        if sigma <= 0:
            raise ValueError("No measured variation")
        gains = mean(max(x, 0) for x in returns[-14:])
        losses = mean(max(-x, 0) for x in returns[-14:])
        rsi = 100 * gains / (gains + losses) if gains + losses else 50
        e12, e26, e50 = ema(closes, 12), ema(closes, 26), ema(closes, 50)
        macd = [a - b for a, b in zip(e12, e26)]
        signal = ema(macd, 9)[-1]
        volume = sum(b["volume"] for b in rows)
        vwap = (
            sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in rows) / volume
            if volume
            else None
        )
        mid = mean(closes[-20:])
        dev = pstdev(closes[-20:])
        short = math.sqrt(sum(x * x for x in returns[-60:]) / (60 * 60))
        ratio = short / sigma
        features = dict(
            return_24h=closes[-1] / rows[0]["open"] - 1,
            rsi_14=rsi,
            macd_scaled=macd[-1] / closes[-1],
            ema_gap=(e12[-1] - e50[-1]) / closes[-1],
            vwap_deviation=closes[-1] / vwap - 1 if vwap else None,
            bollinger_z=(closes[-1] - mid) / dev if dev else None,
            volatility_24h=sigma / closes[-1],
        )
        frames = {}
        for minutes in (1, 5, 15, 60):
            grouped = rows if minutes == 1 else aggregate(rows, minutes)
            frames[str(minutes) + "m"] = {
                "closed_bars": len(grouped),
                "latest_close": grouped[-1]["close"] if grouped else None,
                "momentum": grouped[-1]["close"] / grouped[-2]["close"] - 1
                if len(grouped) > 1
                else None,
                "latest_volume": grouped[-1]["volume"] if grouped else None,
            }
        result.update(
            status="READY",
            reason="24h closed, contiguous proxy history; indicators are correlated candidates, not independent votes",
            first_start=rows[0]["start"],
            last_close=cutoff,
            closed_bars=1440,
            received_at=data["received_at"],
            sigma_dollars_per_sqrt_second=sigma,
            regime="rising volatility"
            if ratio > 1.5
            else "falling volatility"
            if ratio < 2 / 3
            else "similar volatility",
            volatility_ratio=ratio,
            features=features,
            timeframes=frames,
            vwap=vwap,
            volume_24h=volume,
            support=min(b["low"] for b in rows),
            resistance=max(b["high"] for b in rows),
            macd_signal=signal,
            ema12=e12[-1],
            ema26=e26[-1],
            ema50=e50[-1],
            bollinger=[mid - 2 * dev, mid + 2 * dev],
            acceleration=returns[-1] - returns[-2],
            breakout_20m=closes[-1] > max(b["high"] for b in rows[-21:-1]),
            reversal_1m=returns[-1] * returns[-2] < 0,
        )
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        result["reason"] = str(exc)
    return result


def view(state, prior=None, opening=None, generated_at=None):
    market = state.get("market") or {}
    model = state.get("model") or {}
    day = model.get("analysis_24h", {})
    p = state.get("estimate", {}).get("settlement_up")
    try:
        active = (
            timestamp(market["opens_at"])
            <= timestamp(state["now"])
            < timestamp(market["closes_at"])
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        active = False
    ready = (
        active
        and day.get("status") == "READY"
        and not state.get("stale", True)
        and market.get("averaging_rule_verified")
        and market.get("mapping_verified")
        and market.get("comparison_operator") == ">="
        and p is not None
        and math.isfinite(p)
        and 0 <= p <= 1
    )
    result = dict(
        status="EXPERIMENTAL" if ready else "INSUFFICIENT DATA",
        version=VERSION,
        ticker=market.get("ticker"),
        threshold=market.get("strike"),
        closes_at=market.get("closes_at"),
        opens_at=market.get("opens_at"),
        reference=state.get("reference", {}),
        up=p if ready else None,
        down=1 - p if ready else None,
        updated_at=(generated_at.isoformat() if generated_at else state.get("now")),
        data_cutoff=state.get("now"),
        analysis_24h=day,
        regime=day.get("regime", "unavailable"),
        calibration="UNCALIBRATED — experimental model estimate; edge unverified",
        reason="Independent final-average estimate; proxy/index basis and zero-drift assumptions remain unvalidated"
        if ready
        else (state.get("explanation") if day.get("status") == "READY" else day.get("reason"))
        or state.get("explanation")
        or "Fresh supported contract and full 24h history required",
        settlement_condition="UP: final official 60s average >= strike; DOWN: average < strike",
        initial=None,
        previous_up=None,
        change_since_previous=None,
        change_since_initial=None,
    )
    result["initial"] = opening or {
        "status": "INSUFFICIENT DATA",
        "up": None,
        "reason": "No frozen opening inputs available",
    }
    if ready:
        previous = prior.get("latest") if prior else None
        result["previous_up"] = previous["up"] if previous else None
        result["change_since_previous"] = p - previous["up"] if previous else 0
        result["change_since_initial"] = (
            p - opening["up"] if opening and opening.get("up") is not None else None
        )
    return result


def create(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS btc_engine_opening (mode TEXT,ticker TEXT,generated_at TEXT,cutoff TEXT,closes_at TEXT,up REAL,status TEXT,payload TEXT,PRIMARY KEY(mode,ticker))"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS btc_engine_predictions (
        mode TEXT,ticker TEXT,timestamp TEXT,recorded_at TEXT,closes_at TEXT,up REAL,regime TEXT,version TEXT,payload TEXT,
        PRIMARY KEY(mode,ticker,timestamp))"""
    )


def prior(db, ticker, mode):
    rows = db.execute(
        "SELECT timestamp,up,payload FROM btc_engine_predictions WHERE ticker=? AND mode=? ORDER BY timestamp",
        (ticker, mode),
    ).fetchall()
    if not rows:
        return None
    first, last = rows[0], rows[-1]
    return {
        "initial": {
            "timestamp": first[0],
            "up": first[1],
            "opening_delay_seconds": json.loads(first[2]).get("opening_delay_seconds"),
        },
        "latest": {"timestamp": last[0], "up": last[1]},
    }


def record(db, state, wall_now):
    engine = state.get("prediction_engine", {})
    if engine.get("status") != "EXPERIMENTAL" or state.get("stale"):
        return
    mode = state["mode"]
    now = timestamp(engine.get("updated_at", state["now"]))
    close = timestamp(engine["closes_at"])
    if now >= close or (mode == "PUBLIC MARKET DATA" and wall_now >= close):
        return
    if db.execute(
        "SELECT 1 FROM forecast_outcomes WHERE ticker=? AND origin=?",
        (engine["ticker"], "official" if mode == "PUBLIC MARKET DATA" else "replay"),
    ).fetchone():
        return
    payload = {
        "opening_delay_seconds": engine["initial"].get("opening_delay_seconds"),
        "data_cutoff": engine.get("data_cutoff"),
        "model_version": state.get("model", {}).get("version"),
        "threshold": engine["threshold"],
        "baseline_up": state.get("model", {}).get("baseline_probability_up"),
        "market_implied_up": state.get("market_comparison", {}).get("market_implied_up"),
    }
    db.execute(
        "INSERT OR IGNORE INTO btc_engine_predictions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            mode,
            engine["ticker"],
            engine.get("updated_at", state["now"]),
            wall_now.isoformat(),
            engine["closes_at"],
            engine["up"],
            engine["regime"],
            engine["version"],
            json.dumps(payload),
        ),
    )


def performance(db, now):
    grouped = {}
    for row in db.execute(
        """SELECT p.*,o.outcome,o.observed_at FROM btc_engine_predictions p JOIN forecast_outcomes o ON p.ticker=o.ticker
       AND o.origin=CASE WHEN p.mode='PUBLIC MARKET DATA' THEN 'official' ELSE 'replay' END ORDER BY p.timestamp"""
    ):
        if (
            not timestamp(row["timestamp"])
            < timestamp(row["closes_at"])
            <= timestamp(row["observed_at"])
            <= now
        ):
            continue
        if row["mode"] == "PUBLIC MARKET DATA" and timestamp(row["recorded_at"]) >= timestamp(
            row["closes_at"]
        ):
            continue
        grouped.setdefault((row["mode"], row["ticker"]), []).append(dict(row))

    def summarize(groups):
        result = {"settled_markets": len(groups)}
        for which, index in (("initial", 0), ("latest", -1)):
            rows = [g[index] for g in groups]
            if which == "initial":
                initial_rows = []
                for row in rows:
                    opening = db.execute(
                        "SELECT payload FROM btc_engine_opening WHERE mode=? AND ticker=?",
                        (row["mode"], row["ticker"]),
                    ).fetchone()
                    if opening:
                        initial = json.loads(opening[0])
                        if initial.get("up") is not None:
                            initial_rows.append(
                                {
                                    **row,
                                    "up": initial["up"],
                                    "regime": initial.get("regime", "unavailable"),
                                }
                            )
                rows = initial_rows
            missing_initial = len(groups) - len(rows) if which == "initial" else 0
            probs = [r["up"] for r in rows]
            outcomes = [r["outcome"] for r in rows]
            metrics = score(probs, outcomes)
            directional = [r for r in rows if abs(r["up"] - 0.5) > 1e-12]
            correct = sum((r["up"] > 0.5) == bool(r["outcome"]) for r in directional)
            metrics.update(
                missing_initial=missing_initial,
                correct=correct,
                incorrect=len(directional) - correct,
                unclear=len(rows) - len(directional),
                accuracy=correct / len(directional) if directional else None,
                directional_coverage=len(directional) / len(rows) if rows else None,
            )
            metrics["baselines"] = {}
            for key in ("baseline_up", "market_implied_up"):
                matched = [
                    (json.loads(r["payload"]).get(key), r["outcome"])
                    for r in rows
                    if json.loads(r["payload"]).get(key) is not None
                ]
                metrics["baselines"][key] = score([p for p, y in matched], [y for p, y in matched])
            metrics["by_regime"] = {
                regime: score(
                    [r["up"] for r in rows if r["regime"] == regime],
                    [r["outcome"] for r in rows if r["regime"] == regime],
                )
                for regime in sorted({r["regime"] for r in rows})
            }
            metrics["by_probability_range"] = {
                str(lo): score(
                    [
                        r["up"]
                        for r in rows
                        if lo <= max(r["up"], 1 - r["up"]) < (1.000001 if lo == 0.9 else lo + 0.1)
                    ],
                    [
                        r["outcome"]
                        for r in rows
                        if lo <= max(r["up"], 1 - r["up"]) < (1.000001 if lo == 0.9 else lo + 0.1)
                    ],
                )
                for lo in (0.5, 0.6, 0.7, 0.8, 0.9)
            }
            result[which] = metrics
        return result

    result = {}
    for mode in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
        groups = [v for (m, t), v in grouped.items() if m == mode]
        result[mode] = {"all": summarize(groups)}
        for days in (1, 7, 30):
            result[mode][str(days) + "d"] = summarize(
                [g for g in groups if timestamp(g[0]["closes_at"]) >= now - timedelta(days=days)]
            )
    archive = []
    for row in db.execute(
        "SELECT mode,ticker,MAX(timestamp) latest_at,closes_at,COUNT(*) revisions FROM btc_engine_predictions GROUP BY mode,ticker ORDER BY closes_at DESC LIMIT 100"
    ):
        opening = get_opening(db, row["ticker"], row["mode"])
        latest = db.execute(
            "SELECT up,timestamp FROM btc_engine_predictions WHERE mode=? AND ticker=? ORDER BY timestamp DESC LIMIT 1",
            (row["mode"], row["ticker"]),
        ).fetchone()
        outcome = db.execute(
            "SELECT outcome FROM forecast_outcomes WHERE ticker=? AND origin=?",
            (row["ticker"], "official" if row["mode"] == "PUBLIC MARKET DATA" else "replay"),
        ).fetchone()
        archive.append(
            {
                "mode": row["mode"],
                "ticker": row["ticker"],
                "closes_at": row["closes_at"],
                "status": "SETTLED"
                if outcome
                else "AWAITING_OFFICIAL_RESULT"
                if timestamp(row["closes_at"]) <= now
                else "ACTIVE",
                "initial": opening,
                "latest_up": latest[0],
                "latest_at": latest[1],
                "revisions": row["revisions"],
                "outcome": outcome[0] if outcome else None,
            }
        )
    return {
        "archive": archive,
        "by_mode": result,
        "validation": "No validated accuracy or profitable edge claim. Initial/latest per contract; ties excluded from directional accuracy but included in Brier/log loss/calibration. Live and replay never pooled. No historical prediction backfill.",
    }


def opening_forecast(market, history, reference_points, generated_at):
    """Event metadata may arrive late; no BTC observation received after open enters."""
    from .basis import estimate_basis

    opened = timestamp(market["opens_at"])
    closed = timestamp(market["closes_at"])
    result = dict(
        ticker=market["ticker"],
        closes_at=market["closes_at"],
        timestamp=generated_at.isoformat(),
        generated_at=generated_at.isoformat(),
        data_cutoff=opened.isoformat(),
        opening_delay_seconds=max(0, (generated_at - opened).total_seconds()),
        up=None,
        status="INSUFFICIENT DATA",
        reason="No valid frozen opening inputs",
        threshold=market.get("strike"),
        threshold_received_at=market.get("metadata_received_at", market.get("observed_at")),
        regime="unavailable",
        model="opening24h-zero-drift-final-average-v1",
    )
    try:
        if (
            not market.get("mapping_verified")
            or not market.get("averaging_rule_verified")
            or market.get("comparison_operator") != ">="
        ):
            raise ValueError("Unsupported official settlement rules")
        if not 60 <= (closed - opened).total_seconds() <= 900:
            raise ValueError("Incompatible contract duration")
        if timestamp(history["received_at"]) > opened:
            raise ValueError("Historical snapshot received after opening is excluded")
        day = context24(history, opened)
        if day["status"] != "READY":
            raise ValueError(day["reason"])
        points = [
            (t, p) for t, p in reference_points if opened - timedelta(seconds=65) <= t <= opened
        ]
        if not points or (opened - points[-1][0]).total_seconds() > 5:
            raise ValueError("No fresh BTC reference received by opening")
        basis, error = estimate_basis(
            {
                "reference": {
                    "price": points[-1][1],
                    "observed_at": points[-1][0].isoformat(),
                    "official": False,
                },
                "btc_candles": history,
            },
            market,
            points,
            opened,
        )
        if error:
            raise ValueError(error)
        current = points[-1][1]
        strike = number(market["strike"], 0.000001, 1e12)
        sigma = day["sigma_dollars_per_sqrt_second"]
        deviation = sigma * math.sqrt((closed - opened).total_seconds() - 40)
        p = 0.5 * (1 + math.erf((current + basis["point"] - strike) / (math.sqrt(2) * deviation)))
        result.update(
            up=p,
            status="DELAYED" if generated_at > opened else "ON_TIME",
            reason="Frozen pre-opening BTC inputs only; official threshold metadata arrival disclosed; experimental constant-basis Gaussian final-60s-average model",
            reference_received_at=points[-1][0].isoformat(),
            history_received_at=history["received_at"],
            history_last_close=day["last_close"],
            regime=day["regime"],
            basis=basis,
        )
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        result["reason"] = str(exc)
    return result


def get_opening(db, ticker, mode):
    row = db.execute(
        "SELECT payload FROM btc_engine_opening WHERE ticker=? AND mode=?", (ticker, mode)
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_opening(db, mode, opening, wall_now):
    if wall_now >= timestamp(opening["closes_at"]):
        return
    if db.execute(
        "SELECT 1 FROM forecast_outcomes WHERE ticker=? AND origin=?",
        (opening["ticker"], "official" if mode == "PUBLIC MARKET DATA" else "replay"),
    ).fetchone():
        return
    db.execute(
        "INSERT OR IGNORE INTO btc_engine_opening VALUES (?,?,?,?,?,?,?,?)",
        (
            mode,
            opening["ticker"],
            opening["generated_at"],
            opening["data_cutoff"],
            opening["closes_at"],
            opening.get("up"),
            opening["status"],
            json.dumps(opening),
        ),
    )

    saved = get_opening(db, opening["ticker"], mode)
    if saved and saved.get("up") is not None:
        payload = {
            "opening_delay_seconds": saved["opening_delay_seconds"],
            "data_cutoff": saved["data_cutoff"],
            "kind": "opening",
            "threshold": saved.get("threshold"),
            "baseline_up": saved["up"],
        }
        db.execute(
            "INSERT OR IGNORE INTO btc_engine_predictions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                mode,
                saved["ticker"],
                saved["generated_at"],
                wall_now.isoformat(),
                saved["closes_at"],
                saved["up"],
                saved.get("regime", "unavailable"),
                saved["model"],
                json.dumps(payload),
            ),
        )

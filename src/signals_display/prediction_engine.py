"""Past-only 24h context and forward-recorded contract forecasts, not execution."""
from datetime import timedelta
import json
import math
from statistics import mean, pstdev, median
from .model import timestamp, number
from .scores import score, horizon, wilson_interval

VERSION = "btc24h-final-average-v1"
DIRECTION_POLICY = "display-tenth-percent-neutral-v1"


def display_probability_pair(probability):
    """Match browser half-up rounding; the displayed outcomes sum to 100%."""
    try:
        p = number(probability, 0, 1)
    except (ValueError, TypeError):
        return {"up": None, "down": None, "precision_percentage_points": 0.1}
    tenths = math.floor(p * 1000 + 0.5)
    return {"up": tenths / 10, "down": (1000 - tenths) / 10, "precision_percentage_points": 0.1}


def direction_label(probability, unavailable="UNCLEAR"):
    displayed = display_probability_pair(probability)["up"]
    return (
        unavailable
        if displayed is None
        else "UP"
        if displayed > 50
        else "DOWN"
        if displayed < 50
        else "NEUTRAL"
    )


def opening_diagnostics(opening, model_info=None):
    """Explain saved model selection without editing immutable historical rows."""
    opening, model_info = opening or {}, model_info or {}
    selection = opening.get("model_selection", {})
    learning = model_info.get("learning_status", {})
    model = opening.get("model") or "UNRECORDED_OPENING_MODEL"
    fallback = model.startswith("opening24h-zero-drift")
    displayed = display_probability_pair(opening.get("up"))
    audit = selection.get("audit_context", {})
    count = audit.get("official_contracts", learning.get("official_contracts"))
    minimum = audit.get("minimum_contracts", learning.get("minimum_contracts", 170))
    live = learning.get("active", model_info.get("version", "unavailable"))
    return {
        "model_id": model,
        "model_version": model,
        "opening_model": "Experimental zero-drift, constant proxy/index opening-basis Gaussian final-average model"
        if fallback
        else model,
        "live_model": live,
        "live_ensemble_active": live == "ensemble",
        "opening_ensemble_active": False
        if fallback or not selection
        else bool(selection.get("opening_ensemble_active", False)),
        "validated": False,
        "fallback_reason": selection.get("fallback_reason")
        or (
            "No separately validated opening-time model is active. A classifier selected on live-horizon features is not an opening model; post-opening observations are excluded."
            if fallback
            else "Saved opening model metadata does not establish validated predictive accuracy"
        ),
        "candidate_rejection": selection.get("candidate_rejection"),
        "promotion_blocker": (
            f"INSUFFICIENT_OFFICIAL_OUTCOMES: {count}/{minimum} official outcomes before opening-only feature/split checks"
            if isinstance(count, (int, float)) and count < minimum
            else "Opening-only frozen-feature labels and purged out-of-sample calibration must pass before any opening promotion"
        ),
        "official_contracts_at_generation": count,
        "minimum_contracts": minimum,
        "artifact_status": audit.get(
            "artifact_status",
            learning.get("artifact_status", "No opening-specific artifact selected"),
        ),
        "history_available_before_open": opening.get("data_quality", {}).get("closed_history_bars")
        == 1440,
        "closed_history_bars": opening.get("data_quality", {}).get("closed_history_bars"),
        "history_received_at": opening.get("history_received_at"),
        "history_last_close": opening.get("history_last_close"),
        "frozen_feature_status": opening.get("frozen_features", {}).get(
            "status", "NOT_RECORDED_LEGACY_OPENING"
        ),
        "frozen_feature_count": sum(
            v is not None for v in opening.get("frozen_features", {}).get("values", {}).values()
        ),
        "raw_probability_up": opening.get("up"),
        "displayed_probabilities": displayed,
        "display_direction": direction_label(opening.get("up")),
        "saved_direction": opening.get("direction"),
        "direction_policy": DIRECTION_POLICY,
        "near_neutral_reason": (
            "Both probabilities round to 50.0% at the displayed 0.1-percentage-point precision; direction is NEUTRAL. The unrounded estimate is preserved."
            if displayed["up"] == 50
            else None
        ),
        "constant_probability": False if opening.get("probability_inputs") else None,
        "probability_inputs": opening.get("probability_inputs"),
        "probability_explanation": "The fallback is a computed Gaussian event probability, not a hard-coded 50%. With zero drift and a threshold-anchored opening basis, the projected mean is often very close to the threshold."
        if fallback
        else "The opening-specific calibrated classifier uses the saved frozen feature vector; the independent Gaussian baseline is retained separately for evaluation",
        "generated_at": opening.get("generated_at"),
        "data_cutoff": opening.get("data_cutoff"),
        "opening_delay_seconds": opening.get("opening_delay_seconds"),
        "threshold_received_at": opening.get("threshold_received_at"),
        "delay_effect": "A later generation/metadata timestamp does not extend the BTC data cutoff or make a live model eligible at opening",
        "immutable": True,
    }


def _opening_features(history, points, market, day, basis, opened, frozen_context=None):
    """Opening-only learning inputs; no current/live post-opening feature borrow."""
    from .features import FEATURES

    values = {name: None for name in FEATURES}
    values.update({name: value for name, value in day["features"].items() if name in values})
    bars = sorted(
        (
            b
            for b in history["candles"]
            if day["first_start"] <= int(b["start"]) < day["last_close"]
        ),
        key=lambda b: int(b["start"]),
    )
    closes = [float(b["close"]) for b in bars]
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    for minutes in (1, 5, 15):
        if len(closes) > minutes:
            values[f"return_{minutes}m"] = math.log(closes[-1] / closes[-minutes - 1])
    latest = bars[-1]
    o, high, low, close = [float(latest[k]) for k in ("open", "high", "low", "close")]
    span = max(high - low, 1e-9)
    values.update(
        remaining_fraction=1.0,
        distance_scaled=(points[-1][1] + basis["point"] - float(market["strike"])) / points[-1][1],
        acceleration=returns[-1] - returns[-2],
        body_fraction=(close - o) / span,
        upper_wick=(high - max(close, o)) / span,
        lower_wick=(min(close, o) - low) / span,
        range_fraction=(high - low) / o,
        breakout_distance=(close - max(float(b["high"]) for b in bars[-21:-1])) / o,
    )
    old_variance = sum(r * r for r in returns[-20:-5]) / 15
    values["volatility_change"] = (
        (sum(r * r for r in returns[-5:]) / 5) / old_variance if old_variance else None
    )
    old_volume = mean(float(b["volume"]) for b in bars[-21:-1])
    values["relative_volume"] = float(latest["volume"]) / old_volume if old_volume else None
    intervals = [(b[0] - a[0]).total_seconds() for a, b in zip(points, points[1:])]
    observed_span = (points[-1][0] - points[0][0]).total_seconds()
    if observed_span >= 55 and intervals and all(0 < v <= 5 for v in intervals):
        values["micro_1m_return_fraction"] = points[-1][1] / points[0][1] - 1
        values["micro_1m_velocity_dollars_per_second"] = (
            points[-1][1] - points[0][1]
        ) / observed_span
    receipts = [timestamp(history["received_at"]), points[-1][0]]
    optional_status = "UNAVAILABLE: no frozen microstructure context"
    optional_receipts = {}
    if frozen_context:
        try:
            context_cutoff = timestamp(frozen_context["cutoff"])
            if context_cutoff > opened:
                raise ValueError("POST_OPENING_CONTEXT_EXCLUDED")
            if (
                frozen_context.get("max_input_received_at")
                and timestamp(frozen_context["max_input_received_at"]) > opened
            ):
                raise ValueError("POST_OPENING_CONTEXT_INPUT_EXCLUDED")
            optional_values = {}
            feature_receipts = {}
            sources = frozen_context.get("sources", {})
            primary = sources.get(frozen_context.get("primary_source"), {})

            def receipt_for(name, raw):
                receipt = timestamp(raw)
                if receipt > context_cutoff or receipt > opened:
                    raise ValueError("POST_OPENING_SOURCE_RECEIPT_EXCLUDED")
                feature_receipts[name] = {"received_at": receipt.isoformat()}
                return receipt

            for name, value in frozen_context.get("candidate_features", {}).items():
                if name in values and value is not None and name.startswith("micro_"):
                    window_label = name.split("_", 2)[1]
                    window = primary.get("windows", {}).get(window_label, {})
                    if window.get("status") == "READY" and window.get("last_received_at"):
                        receipt_for(name, window["last_received_at"])
                        optional_values[name] = number(value, -1e15, 1e15)
            spot = sources.get("Kraken BTC/USD stream", {})
            if spot.get("status") == "LIVE":
                window = spot.get("windows", {}).get("5s", {})
                if window.get("status") == "READY":
                    imbalance = window.get("features", {}).get("depth_imbalance")
                    if imbalance is not None and window.get("last_received_at"):
                        receipt_for("spot_depth_imbalance", window["last_received_at"])
                        optional_values["spot_depth_imbalance"] = number(imbalance, -1, 1)
            perpetual = sources.get("Kraken futures stream", {})
            if (
                perpetual.get("status") == "LIVE"
                and perpetual.get("latest_price") is not None
                and perpetual.get("data_age_seconds") is not None
            ):
                age = number(perpetual["data_age_seconds"], 0, 5)
                receipt_for(
                    "derivatives_basis", (context_cutoff - timedelta(seconds=age)).isoformat()
                )
                optional_values["derivatives_basis"] = (
                    number(perpetual["latest_price"], 0.000001, 1e12) / points[-1][1] - 1
                )
            advanced = frozen_context.get("advanced_features", {})
            if advanced:
                if timestamp(advanced["cutoff"]) > opened:
                    raise ValueError("POST_OPENING_ADVANCED_CONTEXT_EXCLUDED")
                for name, value in advanced.get("candidate_features", {}).items():
                    if name not in values or not name.startswith("advanced_") or value is None:
                        continue
                    proof = advanced.get("candidate_feature_sources", {}).get(name, {})
                    receipt_for(name, proof["received_at"])
                    if proof.get("event_at") and timestamp(proof["event_at"]) > opened:
                        raise ValueError("POST_OPENING_ADVANCED_EVENT_EXCLUDED")
                    optional_values[name] = number(value, -1e15, 1e15)
            values.update(optional_values)
            receipts.extend(timestamp(item["received_at"]) for item in feature_receipts.values())
            optional_receipts["microstructure"] = {
                "cutoff": context_cutoff.isoformat(),
                "features": feature_receipts,
                "cutoff_note": "Analysis boundary, not an observation receipt timestamp",
            }
            optional_status = "AS_OF_OPENING_CONTEXT: actual used source receipts recorded; analysis cutoff never refreshes an observation"
        except (KeyError, ValueError, TypeError, AttributeError):
            optional_status = "POST_OPENING_OR_UNVERIFIABLE_CONTEXT_EXCLUDED"
    values = {
        name: (float(value) if type(value) in (float, int) and math.isfinite(value) else None)
        for name, value in values.items()
    }
    return {
        "version": "opening-asof-features-v1",
        "status": "FROZEN_AS_OF_OPENING",
        "cutoff": opened.isoformat(),
        "max_input_received_at": max(receipts).isoformat(),
        "values": values,
        "history_received_at": history["received_at"],
        "reference_received_at": points[-1][0].isoformat(),
        "sources": {
            "candles": {"source": history.get("source"), "received_at": history["received_at"]},
            "reference": {
                "received_at": points[-1][0].isoformat(),
                "source_location": "opening.reference_source",
            },
            **optional_receipts,
        },
        "closed_history_bars": day["closed_bars"],
        "optional_context_status": optional_status,
        "target_metadata_received_at": market.get(
            "metadata_received_at", market.get("observed_at")
        ),
        "target_metadata_note": "Official event definition may arrive late; generation is labeled delayed and all BTC observations retain the opening cutoff",
        "independence": "No Kalshi quote/odds input. Derivatives basis, when available, is as-of perpetual trade price versus the observed primary BTC/USD proxy; not pooled with spot volume.",
        "feature_role": "Volatility enters the current fallback. Momentum, volume, liquidity and microstructure are frozen shadow-model candidates, not unvalidated directional boosts.",
    }


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
            vwap_label="Legacy OHLC typical-price weighted approximation; not actual executed-trade VWAP",
            feature_definitions={
                "rsi_14": "Legacy simple-average 14-change RSI; advanced_*_wilder_rsi14 uses Wilder smoothing separately",
                "vwap_deviation": "Legacy candle typical-price weighted approximation; actual-trade VWAP is in advanced_intelligence",
            },
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
        direction=direction_label(p) if ready else "UNCLEAR",
        direction_policy=DIRECTION_POLICY,
        displayed_probabilities=display_probability_pair(p if ready else None),
        confidence={
            "status": "UNCALIBRATED",
            "reason": "Experimental model probabilities; no validated accuracy or profitable edge",
        },
        data_quality={
            "status": "SUPPORTED PROXY INPUTS" if ready else "INSUFFICIENT DATA",
            "history_status": day.get("status", "INSUFFICIENT DATA"),
            "reference_source": state.get("reference", {}).get("source"),
            "reference_received_at": state.get("reference", {}).get("observed_at"),
            "limitation": "Proxy exchange prices and an assumed constant opening basis do not measure the official settlement index",
        },
        settlement_price_estimate={
            "mean": model["price_forecast"]["settlement_average_mean"],
            "conditional_interval": model["price_forecast"].get(
                "settlement_average_conditional_interval"
            ),
            "label": model["price_forecast"].get("interval_label"),
        }
        if ready and model.get("price_forecast", {}).get("settlement_average_mean") is not None
        else None,
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
    result["opening_diagnostics"] = opening_diagnostics(opening, model)
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
    # One small indexed row per contract. Revision blobs never need to be loaded
    # just to list archives or find the current contract's first/latest forecast.
    db.execute(
        """CREATE TABLE IF NOT EXISTS btc_engine_contracts (
        mode TEXT,ticker TEXT,closes_at TEXT,revisions INTEGER NOT NULL DEFAULT 0,
        latest_at TEXT,PRIMARY KEY(mode,ticker))"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS btc_engine_contracts_close ON btc_engine_contracts(closes_at DESC,mode,ticker)"
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS btc_engine_horizons (
        mode TEXT,ticker TEXT,horizon TEXT,timestamp TEXT,recorded_at TEXT,closes_at TEXT,
        up REAL,regime TEXT,version TEXT,payload TEXT,PRIMARY KEY(mode,ticker,horizon))"""
    )
    db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS btc_engine_revision_summary AFTER INSERT ON btc_engine_predictions
        BEGIN
            INSERT INTO btc_engine_contracts(mode,ticker,closes_at,revisions,latest_at)
            VALUES(NEW.mode,NEW.ticker,NEW.closes_at,1,NEW.timestamp)
            ON CONFLICT(mode,ticker) DO UPDATE SET
                revisions=revisions+1,
                latest_at=CASE WHEN latest_at IS NULL OR excluded.latest_at>latest_at THEN excluded.latest_at ELSE latest_at END;
        END;
    """
    )
    db.execute(
        """
        CREATE TRIGGER IF NOT EXISTS btc_engine_opening_summary AFTER INSERT ON btc_engine_opening
        BEGIN
            INSERT OR IGNORE INTO btc_engine_contracts(mode,ticker,closes_at,revisions,latest_at)
            VALUES(NEW.mode,NEW.ticker,NEW.closes_at,0,NULL);
        END;
    """
    )
    # Additive, idempotent migration for journals created before these summaries.
    # Existing immutable predictions and all raw history remain untouched.
    db.execute(
        """INSERT OR IGNORE INTO btc_engine_contracts
        SELECT mode,ticker,closes_at,COUNT(*),MAX(timestamp)
        FROM btc_engine_predictions GROUP BY mode,ticker"""
    )
    db.execute(
        """INSERT OR IGNORE INTO btc_engine_contracts
        SELECT mode,ticker,closes_at,0,NULL FROM btc_engine_opening"""
    )


def prior(db, ticker, mode):
    first = db.execute(
        "SELECT timestamp,up,payload FROM btc_engine_predictions WHERE mode=? AND ticker=? ORDER BY timestamp LIMIT 1",
        (mode, ticker),
    ).fetchone()
    if not first:
        return None
    last = db.execute(
        "SELECT timestamp,up FROM btc_engine_predictions WHERE mode=? AND ticker=? ORDER BY timestamp DESC LIMIT 1",
        (mode, ticker),
    ).fetchone()
    return {
        "initial": {
            "timestamp": first[0],
            "up": first[1],
            "opening_delay_seconds": json.loads(first[2]).get("opening_delay_seconds"),
        },
        "latest": {"timestamp": last[0], "up": last[1]},
    }


def probability_history(db, ticker, mode="PUBLIC MARKET DATA", limit=180, now=None):
    """Bounded chronological chart points, not thousands of raw revision payloads."""
    from datetime import datetime, timezone

    if not isinstance(ticker, str) or not ticker.startswith("KXBTC15M-") or len(ticker) > 120:
        raise ValueError("A KXBTC15M contract ticker is required")
    if mode not in ("PUBLIC MARKET DATA", "REPLAY / SIMULATED"):
        raise ValueError("Unsupported forecast mode")
    if type(limit) is not int or not 1 <= limit <= 180:
        raise ValueError("Chart limit must be 1..180")
    now = now or datetime.now(timezone.utc)
    bins = max(1, limit - 1)
    rows = db.execute(
        """WITH valid AS (
          SELECT timestamp FROM btc_engine_predictions
          WHERE mode=? AND ticker=? AND up BETWEEN 0 AND 1
            AND julianday(timestamp)<julianday(closes_at)
            AND julianday(timestamp)<=julianday(?)
            AND (mode!='PUBLIC MARKET DATA' OR
              (julianday(timestamp)<=julianday(recorded_at)
               AND julianday(recorded_at)<julianday(closes_at)
               AND julianday(recorded_at)<=julianday(?)))
        ), bounds AS (
          SELECT MIN(julianday(timestamp)) lo,MAX(julianday(timestamp)) hi FROM valid
        ), sampled AS (
          SELECT MAX(timestamp) timestamp FROM valid,bounds
          GROUP BY CASE WHEN hi=lo THEN 0 ELSE
            MIN(?,CAST((julianday(timestamp)-lo)/(hi-lo)*? AS INTEGER)) END
        ), chosen AS (
          SELECT timestamp FROM sampled UNION SELECT MIN(timestamp) FROM valid
        )
        SELECT timestamp,recorded_at,up,payload FROM btc_engine_predictions
        WHERE mode=? AND ticker=? AND timestamp IN (SELECT timestamp FROM chosen)
        ORDER BY timestamp DESC LIMIT ?""",
        (mode, ticker, now.isoformat(), now.isoformat(), bins - 1, bins, mode, ticker, limit),
    ).fetchall()
    points = []
    for row in reversed(rows):
        payload = json.loads(row["payload"])
        if payload.get("data_cutoff") and timestamp(payload["data_cutoff"]) > timestamp(
            row["timestamp"]
        ):
            continue
        points.append(
            {
                "timestamp": row["timestamp"],
                "recorded_at": row["recorded_at"],
                "up": row["up"],
                "down": 1 - row["up"],
                "kind": payload.get("kind", "live"),
                "data_cutoff": payload.get("data_cutoff"),
            }
        )
    initial = get_opening(db, ticker, mode)
    if initial and timestamp(initial["generated_at"]) > now:
        initial = None
    return {
        "ticker": ticker,
        "mode": mode,
        "points": points,
        "initial": initial,
        "limit": limit,
        "sampling": "Last observed forecast in each equal-time bucket plus the first observation. No interpolation; missing periods remain missing.",
        "latest_at": points[-1]["timestamp"] if points else None,
    }


def record(db, state, wall_now):
    engine = state.get("prediction_engine", {})
    if engine.get("status") != "EXPERIMENTAL" or state.get("stale"):
        return
    mode = state["mode"]
    now = timestamp(engine.get("updated_at", state["now"]))
    close = timestamp(engine["closes_at"])
    if now >= close or (mode == "PUBLIC MARKET DATA" and (wall_now >= close or now > wall_now)):
        return
    try:
        if timestamp(engine["data_cutoff"]) > now:
            return
        number(engine["up"], 0, 1)
    except (KeyError, ValueError, TypeError, AttributeError):
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
        "opens_at": engine.get("opens_at"),
        "remaining_seconds": (close - now).total_seconds(),
        "horizon": horizon((close - now).total_seconds()),
        "data_quality": engine.get("data_quality", {}),
        "confidence": engine.get("confidence", {}),
        "volatility_regime": state.get("model", {})
        .get("analysis_24h", {})
        .get("regime", "unavailable"),
        "volatility_ratio": state.get("model", {}).get("analysis_24h", {}).get("volatility_ratio"),
        "volatility_dollars_per_sqrt_second": state.get("model", {})
        .get("analysis_24h", {})
        .get("sigma_dollars_per_sqrt_second"),
        "reference_source": state.get("reference", {}).get("source"),
        "reference_received_at": state.get("reference", {}).get("observed_at"),
        "reference_event_at": state.get("reference", {}).get(
            "event_time", state.get("reference", {}).get("event_at")
        ),
        "quote_received_at": (state.get("market") or {}).get("observed_at"),
        "quote_event_at": (state.get("market") or {}).get("event_time"),
        "direction_policy": DIRECTION_POLICY,
        "direction": direction_label(engine["up"]),
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
    _record_horizon(db, mode, engine, wall_now, payload)


def _record_horizon(db, mode, engine, wall_now, payload):
    """Four bounded summary rows per contract, without rereading revision blobs."""
    db.execute(
        """INSERT INTO btc_engine_horizons VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mode,ticker,horizon) DO UPDATE SET
          timestamp=excluded.timestamp,recorded_at=excluded.recorded_at,
          closes_at=excluded.closes_at,up=excluded.up,regime=excluded.regime,
          version=excluded.version,payload=excluded.payload
        WHERE julianday(excluded.timestamp)>julianday(btc_engine_horizons.timestamp)""",
        (
            mode,
            engine["ticker"],
            payload["horizon"],
            engine["updated_at"],
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
    # Start from small per-contract rows and use a primary-key reverse seek for
    # the last eligible revision. Fetching every second's JSON payload was an
    # unnecessary O(all-revisions) Python allocation on each dashboard refresh.
    for row in db.execute(
        """SELECT p.*,o.outcome,o.observed_at,b.payload opening_payload,
            ip.payload initial_score_payload,
            (SELECT json_group_array(json_object(
                'horizon',h.horizon,'timestamp',h.timestamp,'up',h.up,
                'regime',h.regime,'version',h.version,'payload',json(h.payload)))
             FROM btc_engine_horizons h WHERE h.mode=c.mode AND h.ticker=c.ticker
               AND h.up BETWEEN 0 AND 1
               AND julianday(h.timestamp)<julianday(h.closes_at)
               AND julianday(h.closes_at)<=julianday(o.observed_at)
               AND (h.mode!='PUBLIC MARKET DATA' OR
                   (julianday(h.timestamp)<=julianday(h.recorded_at)
                    AND julianday(h.recorded_at)<julianday(h.closes_at)))
            ) horizon_records
        FROM btc_engine_contracts c
        JOIN forecast_outcomes o ON o.ticker=c.ticker
          AND o.origin=CASE WHEN c.mode='PUBLIC MARKET DATA' THEN 'official' ELSE 'replay' END
        JOIN btc_engine_predictions p ON p.rowid=(
            SELECT q.rowid FROM btc_engine_predictions q
            WHERE q.mode=c.mode AND q.ticker=c.ticker
              AND q.up BETWEEN 0 AND 1
              AND julianday(q.timestamp)<julianday(q.closes_at)
              AND (q.mode!='PUBLIC MARKET DATA' OR (julianday(q.timestamp)<=julianday(q.recorded_at) AND julianday(q.recorded_at)<julianday(q.closes_at)))
              AND julianday(q.closes_at)<=julianday(o.observed_at)
            ORDER BY q.timestamp DESC LIMIT 1)
        LEFT JOIN btc_engine_opening b ON b.mode=c.mode AND b.ticker=c.ticker
        LEFT JOIN btc_engine_predictions ip ON ip.mode=b.mode AND ip.ticker=b.ticker AND ip.timestamp=b.generated_at
        WHERE julianday(o.observed_at)<=julianday(?)""",
        (now.isoformat(),),
    ):
        record = dict(row)
        record["score_payload"] = json.loads(record["payload"])
        record["opening"] = (
            json.loads(record["opening_payload"]) if record["opening_payload"] else None
        )
        record["initial_score_payload"] = (
            json.loads(record["initial_score_payload"]) if record["initial_score_payload"] else {}
        )
        record["horizon_records"] = json.loads(record["horizon_records"] or "[]")
        grouped[(row["mode"], row["ticker"])] = [record]

    def summarize(groups):
        result = {"settled_markets": len(groups)}
        for which, index in (("initial", 0), ("latest", -1)):
            rows = [g[index] for g in groups]
            if which == "initial":
                initial_rows = []
                for row in rows:
                    initial = row.get("opening")
                    if (
                        initial
                        and initial.get("up") is not None
                        and timestamp(initial["generated_at"]) < timestamp(row["closes_at"])
                        and timestamp(initial["data_cutoff"])
                        <= timestamp(initial.get("opens_at", initial["data_cutoff"]))
                    ):
                        initial_rows.append(
                            {
                                **row,
                                "up": initial["up"],
                                "regime": initial.get("regime", "unavailable"),
                                "score_payload": row["initial_score_payload"],
                            }
                        )
                rows = initial_rows
            missing_initial = len(groups) - len(rows) if which == "initial" else 0
            probs = [r["up"] for r in rows]
            outcomes = [r["outcome"] for r in rows]
            metrics = score(probs, outcomes)
            directional = [r for r in rows if direction_label(r["up"]) in {"UP", "DOWN"}]
            correct = sum(
                (direction_label(r["up"]) == "UP") == bool(r["outcome"]) for r in directional
            )
            metrics.update(
                missing_initial=missing_initial,
                correct=correct,
                incorrect=len(directional) - correct,
                unclear=len(rows) - len(directional),
                accuracy=correct / len(directional) if directional else None,
                accuracy_interval=wilson_interval(correct, len(directional)),
                directional_coverage=len(directional) / len(rows) if rows else None,
                directional_samples=len(directional),
                directional_accuracy=correct / len(directional) if directional else None,
                directional_accuracy_interval=wilson_interval(correct, len(directional)),
                unclear_samples=len(rows) - len(directional),
                neutral_policy=DIRECTION_POLICY,
            )
            metrics["baselines"] = {}
            for key in ("baseline_up", "market_implied_up"):
                matched = [
                    (r["score_payload"].get(key), r["outcome"])
                    for r in rows
                    if r["score_payload"].get(key) is not None
                ]
                metrics["baselines"][key] = score([p for p, y in matched], [y for p, y in matched])
            metrics["by_regime"] = {
                regime: _evaluate_rows([r for r in rows if r["regime"] == regime])
                for regime in sorted({r["regime"] for r in rows})
            }
            metrics["by_probability_range"] = {
                str(lo): _evaluate_rows(
                    [
                        r
                        for r in rows
                        if lo <= max(r["up"], 1 - r["up"]) < (1.000001 if lo == 0.9 else lo + 0.1)
                    ]
                )
                for lo in (0.5, 0.6, 0.7, 0.8, 0.9)
            }
            metrics["by_volatility_regime"] = _score_groups(
                rows, lambda r: r["score_payload"].get("volatility_regime", "unavailable")
            )
            metrics["by_data_quality"] = _score_groups(
                rows,
                lambda r: r["score_payload"].get("data_quality", {}).get("status", "unavailable"),
            )
            result[which] = metrics
        horizon_rows = []
        for group in groups:
            latest = group[-1]
            for item in latest.get("horizon_records", []):
                horizon_rows.append(
                    {**item, "outcome": latest["outcome"], "score_payload": item["payload"]}
                )
        result["by_time_remaining"] = {
            band: _evaluate_rows([r for r in horizon_rows if r["horizon"] == band])
            for band in (">10m", "5-10m", "2-5m", "<=2m")
        }
        result[
            "horizon_sampling"
        ] = "Last recorded eligible live forecast per contract/time-remaining band. Four incremental summary rows per contract; legacy revisions are not backfilled into new horizons."
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
        """SELECT c.*,b.payload opening_payload,p.up latest_up,p.timestamp eligible_latest_at,
            p.payload latest_payload,o.outcome
        FROM (SELECT * FROM btc_engine_contracts ORDER BY closes_at DESC LIMIT 100) c
        LEFT JOIN btc_engine_opening b ON b.mode=c.mode AND b.ticker=c.ticker
        LEFT JOIN btc_engine_predictions p ON p.rowid=(
            SELECT q.rowid FROM btc_engine_predictions q
            WHERE q.mode=c.mode AND q.ticker=c.ticker AND q.up BETWEEN 0 AND 1
              AND julianday(q.timestamp)<julianday(q.closes_at)
              AND (q.mode!='PUBLIC MARKET DATA' OR
                   (julianday(q.timestamp)<=julianday(q.recorded_at)
                    AND julianday(q.recorded_at)<julianday(q.closes_at)))
            ORDER BY q.timestamp DESC LIMIT 1)
        LEFT JOIN forecast_outcomes o ON o.ticker=c.ticker
          AND o.origin=CASE WHEN c.mode='PUBLIC MARKET DATA' THEN 'official' ELSE 'replay' END
          AND julianday(o.observed_at)<=julianday(?)
        ORDER BY c.closes_at DESC""",
        (now.isoformat(),),
    ):
        outcome = row["outcome"]
        initial = json.loads(row["opening_payload"]) if row["opening_payload"] else None
        latest_payload = json.loads(row["latest_payload"]) if row["latest_payload"] else {}
        initial_up = initial.get("up") if initial else None
        latest_up = row["latest_up"]
        archive.append(
            {
                "mode": row["mode"],
                "ticker": row["ticker"],
                "closes_at": row["closes_at"],
                "status": "SETTLED"
                if outcome is not None
                else "AWAITING_OFFICIAL_RESULT"
                if timestamp(row["closes_at"]) <= now
                else "ACTIVE",
                "initial": initial,
                "opens_at": initial.get("opens_at") if initial else latest_payload.get("opens_at"),
                "threshold": initial.get("threshold")
                if initial
                else latest_payload.get("threshold"),
                "opening_correct": (direction_label(initial_up) == "UP") == bool(outcome)
                if outcome is not None and direction_label(initial_up) in {"UP", "DOWN"}
                else None,
                "opening_data_quality": initial.get("data_quality") if initial else None,
                "latest_up": latest_up,
                "latest_down": 1 - latest_up if latest_up is not None else None,
                "latest_at": row["eligible_latest_at"],
                "latest_correct": (direction_label(latest_up) == "UP") == bool(outcome)
                if outcome is not None and direction_label(latest_up) in {"UP", "DOWN"}
                else None,
                "latest_data_quality": latest_payload.get("data_quality"),
                "latest_confidence": latest_payload.get("confidence"),
                "revisions": row["revisions"],
                "outcome": outcome,
            }
        )

    cycle_counts = [
        dict(row)
        for row in db.execute(
            """SELECT c.mode,COUNT(*) total,
          SUM(julianday(c.closes_at)<=julianday(?)) closed,
          SUM(CASE WHEN julianday(o.observed_at)<=julianday(?) THEN 1 ELSE 0 END) settled
        FROM btc_engine_contracts c LEFT JOIN forecast_outcomes o ON o.ticker=c.ticker
          AND o.origin=CASE WHEN c.mode='PUBLIC MARKET DATA' THEN 'official' ELSE 'replay' END
        GROUP BY c.mode""",
            (now.isoformat(), now.isoformat()),
        )
    ]
    for counts in cycle_counts:
        mode_metrics = result.get(counts["mode"])
        if mode_metrics:
            scored = mode_metrics["all"]["settled_markets"]
            mode_metrics["recorded_cycles"] = {
                **counts,
                "settled_without_supported_live": max(0, counts["settled"] - scored),
                "settled_missing_initial": max(
                    0, counts["settled"] - mode_metrics["all"]["initial"]["samples"]
                ),
            }
    return {
        "archive": archive,
        "archive_limit": 100,
        "archive_total": sum(c["total"] for c in cycle_counts),
        "by_mode": result,
        "direction_scoring_policy": DIRECTION_POLICY,
        "validation": "No validated accuracy or profitable edge claim. Initial/latest per contract; forecasts that display 50.0/50.0 are NEUTRAL and excluded from directional accuracy but included at original raw precision in Brier/log loss/calibration. Immutable historical raw probabilities and saved labels are unchanged. Live and replay never pooled. No historical prediction backfill.",
    }


def _evaluate_rows(rows):
    metrics = score([r["up"] for r in rows], [r["outcome"] for r in rows])
    directional = [r for r in rows if direction_label(r["up"]) in {"UP", "DOWN"}]
    correct = sum((direction_label(r["up"]) == "UP") == bool(r["outcome"]) for r in directional)
    metrics.update(
        correct=correct,
        incorrect=len(directional) - correct,
        accuracy=correct / len(directional) if directional else None,
        accuracy_interval=wilson_interval(correct, len(directional)),
        unclear=len(rows) - len(directional),
        directional_samples=len(directional),
        directional_accuracy=correct / len(directional) if directional else None,
        directional_accuracy_interval=wilson_interval(correct, len(directional)),
        unclear_samples=len(rows) - len(directional),
        neutral_policy=DIRECTION_POLICY,
    )
    metrics["baselines"] = {}
    for key in ("baseline_up", "market_implied_up"):
        matched = [
            (r["score_payload"].get(key), r["outcome"])
            for r in rows
            if r["score_payload"].get(key) is not None
        ]
        metrics["baselines"][key] = score([p for p, _ in matched], [y for _, y in matched])
    return metrics


def _score_groups(rows, grouping):
    values = {grouping(r) or "unavailable" for r in rows}
    return {
        key: _evaluate_rows([r for r in rows if (grouping(r) or "unavailable") == key])
        for key in sorted(values)
    }


def opening_forecast(
    market,
    history,
    reference_points,
    generated_at,
    reference_source=None,
    audit_context=None,
    frozen_context=None,
    candidate_artifact=None,
):
    """Event metadata may arrive late; no BTC observation received after open enters."""
    from .basis import estimate_basis

    opened = timestamp(market["opens_at"])
    closed = timestamp(market["closes_at"])
    result = dict(
        ticker=market["ticker"],
        opens_at=market["opens_at"],
        closes_at=market["closes_at"],
        timestamp=generated_at.isoformat(),
        generated_at=generated_at.isoformat(),
        data_cutoff=opened.isoformat(),
        opening_delay_seconds=max(0, (generated_at - opened).total_seconds()),
        up=None,
        down=None,
        direction="UNCLEAR",
        settlement_price_estimate=None,
        confidence={
            "status": "UNCALIBRATED",
            "reason": "Experimental model probabilities; no validated accuracy or profitable edge",
        },
        data_quality={"status": "INSUFFICIENT DATA"},
        status="INSUFFICIENT DATA",
        reason="No valid frozen opening inputs",
        threshold=market.get("strike"),
        threshold_received_at=market.get("metadata_received_at", market.get("observed_at")),
        regime="unavailable",
        model="opening24h-zero-drift-final-average-v1",
        direction_policy=DIRECTION_POLICY,
        model_selection={
            "opening_ensemble_active": False,
            "status": "EXPERIMENTAL_OPENING_FALLBACK",
            "fallback_reason": "No separately validated opening-time artifact is active. The live-horizon learner cannot be reused with post-opening features; the frozen zero-drift statistical model remains the opening fallback.",
            "audit_context": {
                k: v
                for k, v in (audit_context or {}).items()
                if k
                in {
                    "official_contracts",
                    "minimum_contracts",
                    "artifact_status",
                    "live_model",
                    "live_ensemble_active",
                    "opening_candidate_status",
                }
            },
            "historical_feature_role": "24h volatility is used; directional indicator coefficients are not validated. Correlated trend/momentum/volume features are collected for opening-only shadow evaluation.",
        },
    )
    try:
        if not opened <= generated_at < closed:
            raise ValueError(
                "Opening forecast must be generated during the active contract, never before opening or after settlement"
            )
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
        by_time = {}
        for t, raw_price in reference_points:
            if not opened - timedelta(seconds=65) <= t <= opened:
                continue
            price = number(raw_price, 0.000001, 1e12)
            if t in by_time and by_time[t] != price:
                raise ValueError(
                    "Conflicting pre-opening reference observations at the same timestamp"
                )
            by_time[t] = price
        points = sorted(by_time.items())
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
            down=1 - p,
            direction=direction_label(p),
            displayed_probabilities=display_probability_pair(p),
            probability_inputs={
                "method": "Gaussian final-average CDF, zero drift",
                "projected_settlement_average": current + basis["point"],
                "official_threshold": strike,
                "projected_mean_minus_threshold": current + basis["point"] - strike,
                "standard_deviation_final_average": deviation,
                "z_score": (current + basis["point"] - strike) / deviation,
                "sigma_dollars_per_sqrt_second": sigma,
                "constant_proxy_index_basis": basis["point"],
                "proxy_opening_average": basis.get("proxy_opening_average"),
                "hard_coded_probability": False,
            },
            settlement_price_estimate={
                "mean": current + basis["point"],
                "conditional_interval": [
                    current + basis["low"] - 1.96 * deviation,
                    current + basis["high"] + 1.96 * deviation,
                ],
                "label": "Experimental zero-drift projected final-60s official-index average using an assumed constant proxy/index opening basis; Gaussian 95% conditional range is NOT calibrated coverage",
            },
            reference_price=current,
            reference_source=reference_source or "BTC proxy feed (sample source not recorded)",
            data_quality={
                "status": "SUPPORTED PROXY INPUTS",
                "closed_history_bars": day["closed_bars"],
                "history_received_at": history["received_at"],
                "reference_received_at": points[-1][0].isoformat(),
                "reference_source": reference_source
                or "BTC proxy feed (sample source not recorded)",
                "reference_age_at_open_seconds": (opened - points[-1][0]).total_seconds(),
                "limitation": "Proxy exchange prices and an assumed constant opening basis do not measure the official settlement index",
            },
            status="DELAYED" if generated_at > opened else "ON_TIME",
            reason="Frozen pre-opening BTC inputs only; official threshold metadata arrival disclosed; experimental constant-basis Gaussian final-60s-average model",
            reference_received_at=points[-1][0].isoformat(),
            history_received_at=history["received_at"],
            history_last_close=day["last_close"],
            regime=day["regime"],
            basis=basis,
            volatility_ratio=day.get("volatility_ratio"),
            frozen_features=_opening_features(
                history, points, market, day, basis, opened, frozen_context
            ),
        )
        result["baseline_probability_up"] = p
        if candidate_artifact is not None:
            from .opening_models import eligible_at_opening, predict as candidate_predict

            if eligible_at_opening(candidate_artifact, opened):
                candidate_p = number(
                    candidate_predict(
                        candidate_artifact["model"],
                        result["frozen_features"]["values"],
                        baseline_probability=p,
                    ),
                    0,
                    1,
                )
                result.update(
                    up=candidate_p,
                    down=1 - candidate_p,
                    direction=direction_label(candidate_p),
                    displayed_probabilities=display_probability_pair(candidate_p),
                    model="opening-candidate/" + candidate_artifact["model_id"],
                    reason="Explicitly configured opening-only candidate passed chronological probabilistic gates; frozen as-of-opening features only; experimental, edge unverified",
                )
                result["model_selection"].update(
                    status="EXPERIMENTAL_OPENING_CANDIDATE",
                    opening_ensemble_active=candidate_artifact["model"]["kind"]
                    == "opening_ensemble",
                    candidate_kind=candidate_artifact["model"]["kind"],
                    fallback_reason="No fallback: explicit opening-specific candidate was trained, available and loaded before cutoff; statistical gates do not establish a profitable edge",
                    model_id=candidate_artifact["model_id"],
                    labels_available_through=candidate_artifact["labels_available_through"],
                    available_at=candidate_artifact["available_at"],
                    loaded_at=candidate_artifact["runtime_loaded_at"],
                    validation_status=candidate_artifact["report"]["status"],
                )
                result["confidence"] = {
                    "status": "EXPERIMENTAL CALIBRATED CANDIDATE",
                    "reason": "Validation-only calibration and opening-only held-out statistical gates passed; live calibration, model/basis error and profitable edge remain unverified",
                }
                result["settlement_price_estimate"][
                    "label"
                ] += "; price distribution is the independent statistical baseline, not the selected binary classifier"
            else:
                from .opening_models import validate_artifact

                blocker = "OPENING_ARTIFACT_SCHEMA_OR_STATISTICAL_GATES_UNVERIFIED"
                try:
                    validate_artifact(candidate_artifact)
                    if timestamp(candidate_artifact["labels_available_through"]) >= opened:
                        blocker = "OPENING_ARTIFACT_LABELS_UNAVAILABLE_AT_OPENING"
                    elif timestamp(candidate_artifact["created_at"]) > opened:
                        blocker = "OPENING_ARTIFACT_CREATED_AFTER_OPENING"
                    elif timestamp(candidate_artifact["available_at"]) > opened:
                        blocker = "OPENING_ARTIFACT_AVAILABLE_AFTER_OPENING"
                    elif not candidate_artifact.get("runtime_loaded_at"):
                        blocker = "OPENING_ARTIFACT_RECEIPT_NOT_RECORDED"
                    elif timestamp(candidate_artifact["runtime_loaded_at"]) > opened:
                        blocker = "OPENING_ARTIFACT_LOADED_AFTER_OPENING"
                except (KeyError, ValueError, TypeError, AttributeError, RecursionError):
                    pass
                rejection = (
                    "OPENING_ARTIFACT_INELIGIBLE: "
                    + blocker
                    + "; delayed generation never advances the model cutoff"
                )
                result["model_selection"]["candidate_rejection"] = rejection
                result["model_selection"]["fallback_reason"] = (
                    rejection + "; retaining the frozen statistical fallback"
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
            "baseline_up": saved.get("baseline_probability_up", saved["up"]),
            "direction": direction_label(saved["up"]),
            "direction_policy": DIRECTION_POLICY,
            "opens_at": saved.get("opens_at"),
            "data_quality": saved.get("data_quality", {}),
            "confidence": saved.get("confidence", {}),
            "volatility_regime": saved.get("regime", "unavailable"),
            "volatility_ratio": saved.get("volatility_ratio"),
            "reference_source": saved.get("reference_source"),
            "reference_received_at": saved.get("reference_received_at"),
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

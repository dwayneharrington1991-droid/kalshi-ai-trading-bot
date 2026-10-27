"""Observable microstructure candidates. No inferred institutions or probability boosts."""
from statistics import median
from .model import timestamp, number


def flow_metrics(trades, started, now):
    rows = [t for t in trades if 0 <= (now - t[1]).total_seconds() <= 180]
    ready = bool(
        started
        and (now - started).total_seconds() >= 180
        and len(rows) >= 60
        and (now - rows[0][1]).total_seconds() >= 175
        and (now - rows[-1][1]).total_seconds() <= 5
    )
    out = dict(
        status="LIVE" if ready else "unavailable",
        net_volume_3m=None,
        total_volume_3m=None,
        price_change_3m=None,
        delta_efficiency=None,
        volume_taper_ratio=None,
        fragile_upward_impact_candidate=None,
        reason="Requires 180s uninterrupted session, >=60 observed trades, >=175s coverage and fresh final trade",
    )
    if not ready:
        return out
    total = sum(t[3] for t in rows)
    net = sum(t[3] if t[4] == "buy" else -t[3] for t in rows)
    change = rows[-1][2] - rows[0][2]
    latest = sum(t[3] for t in rows if (now - t[1]).total_seconds() <= 60)
    previous = sum(t[3] for t in rows if 60 < (now - t[1]).total_seconds() <= 120)
    taper = latest / previous if previous else None
    fraction = net / total if total else 0
    # Near-zero net volume produces unstable/infinite ratios; report unavailable.
    dem = change / net if abs(fraction) >= 0.05 and net else None
    out.update(
        net_volume_3m=net,
        total_volume_3m=total,
        price_change_3m=change,
        delta_efficiency=dem,
        volume_taper_ratio=taper,
        fragile_upward_impact_candidate=bool(
            0.05 <= fraction <= 0.20
            and change / rows[0][2] >= 0.001
            and taper is not None
            and taper < 0.7
        ),
        reason="Experimental dollars per signed venue quantity; denominator withheld below 5% net/gross volume. Upward-impact candidate: >=10bp price rise, 5-20% net buy fraction, final-minute gross volume <70% of preceding minute. This does not establish low liquidity or future exhaustion.",
    )
    return out


def fresh(data, now, seconds=5):
    try:
        return 0 <= (now - timestamp(data["observed_at"])).total_seconds() <= seconds
    except (KeyError, TypeError, ValueError):
        return False


def assess(payload, market, info, now):
    signatures = []
    flow = payload.get("coverage", {}).get("Kraken public order flow", {}).get("data", {})
    for venue in ("spot", "perpetual"):
        item = flow.get(venue, {})
        if item.get("status") != "LIVE" or not fresh(item, now):
            continue
        if item.get("passive_sell_absorption_candidate"):
            signatures.append(
                dict(
                    kind="Bearish passive absorption candidate",
                    venue=venue,
                    bias="DOWN",
                    reason="Aggressive buying with flat price; seller identity/icebergs and subsequent direction cannot be determined.",
                )
            )
        if item.get("bearish_divergence_candidate"):
            signatures.append(
                dict(
                    kind="Bearish CVD divergence candidate",
                    venue=venue,
                    bias="DOWN",
                    reason="Confirmed higher price peak with lower CVD; experimental, no entry implied.",
                )
            )
        dem = item.get("delta_efficiency_3m", {})
        if dem.get("status") == "LIVE" and dem.get("fragile_upward_impact_candidate"):
            signatures.append(
                dict(
                    kind="Fragile upward price-impact candidate",
                    venue=venue,
                    bias="DOWN",
                    reason="Large upward price change relative to net buy volume with tapering gross volume; not proof of an illiquid pump.",
                )
            )
    levels = {"previous_day": None, "session": None, "short_term": None}
    reasons = []
    daily = payload.get("coverage", {}).get("Coinbase UTC daily levels", {})
    if daily.get("status") == "CONNECTED" and fresh(daily.get("data", {}), now, 150):
        levels["previous_day"] = daily["data"].get("previous_day")
        levels["session"] = daily["data"].get("session")
    else:
        reasons.append("Previous-day/full UTC-session levels unavailable: daily feed missing/stale")
    # Revalidate candidate bars independently; malformed/future data cannot make flags.
    bars = []
    try:
        if not fresh({"observed_at": payload["btc_candles"]["received_at"]}, now, 150):
            raise ValueError("stale candle response")
        for raw in info.get("candidate_features", {}).get("closed_candles", []):
            b = {
                k: number(raw[k], 0 if k == "volume" else 0.000001, 1e15)
                for k in ("open", "high", "low", "close", "volume")
            }
            b["start"] = int(raw["start"])
            if (
                b["start"] % 60
                or b["start"] + 60 > now.timestamp()
                or not b["low"]
                <= min(b["open"], b["close"])
                <= max(b["open"], b["close"])
                <= b["high"]
            ):
                raise ValueError("invalid/future candle")
            bars.append(b)
        if (
            len(bars) < 21
            or now.timestamp() - bars[-1]["start"] - 60 > 150
            or any(b["start"] - a["start"] != 60 for a, b in zip(bars, bars[1:]))
        ):
            raise ValueError("need 21 contiguous recent closed one-minute bars")
        prior, last = bars[-21:-1], bars[-1]
        high = max(b["high"] for b in prior)
        low = min(b["low"] for b in prior)
        levels["short_term"] = dict(
            high=high,
            low=low,
            as_of=last["start"],
            source="Coinbase closed 20m proxy range excluding sweep candle",
        )
        typical = median(b["high"] - b["low"] for b in prior)
        threshold = max(0.0001 * last["open"], 0.25 * typical)
        avgvol = sum(b["volume"] for b in prior[-5:]) / 5
        declining = avgvol > 0 and last["volume"] < avgvol and last["volume"] < prior[-1]["volume"]
        violent = typical > 0 and last["high"] - last["low"] >= 1.5 * typical
        candidates = [("short-term", high, low)]
        if levels["previous_day"]:
            candidates.append(
                ("previous UTC day", levels["previous_day"]["high"], levels["previous_day"]["low"])
            )
        # Full current-session high/low includes the sweep, so cannot be a pre-sweep benchmark.
        session = [b for b in bars[:-1] if b["start"] >= int(now.timestamp()) // 86400 * 86400]
        if session and session[0]["start"] == int(now.timestamp()) // 86400 * 86400:
            candidates.append(
                (
                    "pre-sweep UTC session",
                    max(b["high"] for b in session),
                    min(b["low"] for b in session),
                )
            )
        if declining and violent:
            for name, h, l in candidates:
                if last["high"] >= h + threshold and last["close"] < h:
                    signatures.append(
                        dict(
                            kind="Low-volume upper sweep/reclaim candidate",
                            venue="Coinbase proxy",
                            bias="DOWN",
                            level=h,
                            level_source=name,
                            reason="Closed candle sweeps prior high then closes below it on declining volume; stop executions and 70% probability are unverified.",
                        )
                    )
                if last["low"] <= l - threshold and last["close"] > l:
                    signatures.append(
                        dict(
                            kind="Low-volume lower sweep/reclaim candidate",
                            venue="Coinbase proxy",
                            bias="UP",
                            level=l,
                            level_source=name,
                            reason="Closed candle sweeps prior low then closes above it on declining volume; stop executions and 70% probability are unverified.",
                        )
                    )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        reasons.append("Liquidity-sweep analysis unavailable: " + str(exc))
    pin = dict(
        status="unavailable",
        reason="Kalshi depth/open interest does not identify market makers, hidden orders or strike-clustered ownership. No pinning probability/bias is inferred.",
    )
    try:
        if market and fresh(market, now) and fresh(payload.get("reference", {}), now):
            remaining = (timestamp(market["closes_at"]) - now).total_seconds()
            price = number(payload["reference"]["price"], 0.000001, 1e12)
            strike = number(market["strike"], 0.000001, 1e12)
            pin.update(
                final_three_minutes=0 < remaining <= 180,
                distance_to_target_bps=10000 * (price / strike - 1),
                reference_is_proxy=not payload["reference"].get("official", False),
            )
    except (KeyError, TypeError, ValueError):
        pass
    if not market or not fresh(market, now) or not fresh(payload.get("reference", {}), now):
        signatures = []
        reasons.append("Fresh unique contract and BTC reference required for active signatures")
    biases = {s["bias"] for s in signatures}
    lean = next(iter(biases)) if len(biases) == 1 else "UNCLEAR"
    summary = (
        "; ".join(dict.fromkeys(s["kind"] for s in signatures))
        if signatures
        else "unavailable: " + "; ".join(reasons)
        if reasons
        else "No supported active signature"
    )
    return dict(
        line="Edge Reading Core: " + summary + " — experimental; edge unverified",
        signatures=signatures,
        candidate_lean=lean,
        probability=None,
        probability_adjustment=0.0,
        observed_at=now.isoformat(),
        levels=levels,
        pinning=pin,
        unavailable_reasons=reasons,
        limitations="No institution/iceberg attribution, inferred stop fills, forced entries, 70% floor or arbitrary confidence bias. Spot/perpetual disagreement remains UNCLEAR. These diagnostics do not qualify trades or modify fallback forecasts.",
    )
